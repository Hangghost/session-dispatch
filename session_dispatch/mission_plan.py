"""Mission plan — 指揮站派工的作戰圖讀寫與完成訊號求值。

English summary: deterministic core for the session-dispatch workflow. Reads and
writes a mission plan (plain JSON), evaluates each node's completion signal as a
three-state outcome, computes which nodes are ready to dispatch, and lints the
brief that gets handed to each worker. No LLM calls, no network.

## 這個 module 刻意不是什麼

**它不是 task graph 引擎。** 沒有 provenance、沒有確定性 id、沒有重算器、沒有衝突
收斂。它服務的是**一輪對話級的短命 fan-out**：三到五個節點、machine-local、做完即棄。

需要跨月存活、跨機同步、可視化的依賴圖，那是另一種東西——機制相似，但壽命與目的
不同，用這個 module 去長成那個會得到一個兩邊都做不好的中間物。

## 三個不變式

1. **plan 檔只由指揮站寫。** worker SHALL NOT 寫入——單一寫入者消除並發，且使完成
   判定不依賴 worker 記得回報（LLM 不是可靠的 bookkeeper）。
2. **完成判定的唯一權威是 `done_signal`**，不是 worker 的訊息。訊息至多是喚醒訊號。
3. **求值失敗 SHALL NOT 併入「未完成」**。`unavailable` 是顯式第三態：「這條指令跑
   不起來」與「這件事還沒做完」是兩回事，共用一個回傳值就是靜默降級。

## 跨 repo 節點

worker 的 workspace 可以是**另一個 git repository**（驅動情境：這個 repo 當指揮站，
worker 在某個 code repo 裡跑那個 repo 自己的工作流）。三條連帶語意：

- **求值錨點是 per-node 的**（`MissionNode.workspace_repo`），不是 mission-wide 的 cwd。
  一份 mission 可以混合兩種節點，以單一 cwd 求值全部節點必然錯一半。
- **plan 與派工單恆留在指揮站。** worker 以顯式的附加目錄授權讀取，SHALL NOT 把 mission
  目錄複製進目標 repo——分散存放會讓同一次 dispatch 有 N 份作戰圖，而它們會各自漂移。
- **跨 repo 節點的 `done_signal` 走 commit 形態。** 容器名由目標 repo 自己的工作流決定，
  指揮站推不準（見 `worker_output_dir()` 的拒答理由）。

規約 SSOT：``SPEC.md``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Optional

#: 環境變數：覆寫 mission 目錄的錨點。未設定時錨定 main checkout 的 `.claude/missions/`。
#:
#: 存在理由是本工作流不假設你的 repo 長什麼樣。預設值選 `.claude/` 之下，是因為
#: mission 檔是 machine-local 的過程產物——它們**不該進 git**（見 README 的
#: `.gitignore` 建議），與 Claude Code 自己的 session 產物同一性質。
HOME_ENV_VAR = "SESSION_DISPATCH_HOME"

#: `done_signal` 求值逾時。訊號是輔助判定,不值得讓指揮站卡住。
_SIGNAL_TIMEOUT_SECONDS = 30

#: 節點的合法狀態。`abandoned` 與 `done` 同屬終止態,差別在產物是否可用。
NODE_STATUSES = ("pending", "dispatched", "done", "abandoned")

#: 節點的執行形態。`session` 是具名的獨立 agent session(自己的 worktree 與分支);
#: `subagent` 由指揮站 spawn,與指揮站共用檔案系統與生命週期,結果直接回 caller。
#:
#: 形態決定哪些條文適用:綁 session 的前提(跨 worktree 寫入隔離、以名稱定址的訊息
#: 通道、獨立分支生命週期)在 subagent 形態下不存在,故 `done_signal` / `base_ref` /
#: 撞名檢查 / worktree 處置一律不套用。但**派工單五元件與產物落點驗證跨形態通用**
#: ——實跑證明「agent 回報寫好了」與「寫在你以為的地方」是兩件事,該風險與形態無關。
NODE_SHAPES = ("session", "subagent")

#: 訊號求值的三態。`unavailable` 刻意與 `pending` 分離(見 module docstring 不變式 3)。
SIGNAL_STATES = ("done", "pending", "unavailable")

#: worker 容器處置的三態。與 `SIGNAL_STATES` **刻意分開宣告**:兩者的 `unavailable` 語意
#: 相同(「查不到」不是「沒做完」),但另外兩態問的是不同的問題——訊號問「工作做完了嗎」,
#: 處置問「容器還在嗎」。共用同一組常數會讓兩個概念在呼叫點看起來可以互換,而
#: `done` × `pending` 的組合(工作做完了、容器還佔著)正是本能力要讓人看見的那個狀態。
TEARDOWN_STATES = ("disposed", "pending", "unavailable")

#: 語意無歧義地表示「這條指令跑不起來」的退出碼:127 找不到指令、126 找到了但不可執行。
#: 刻意**不**含 git 的 128/129——那兩個同時用於「不是有效物件」,升級它們會把「分支還沒
#: 出現」誤報成壞掉的訊號(那是第三態的反向誤用)。見 `evaluate_signal()` 的射程說明。
_UNRUNNABLE_EXIT_CODES = frozenset({126, 127})


# --------------------------------------------------------------------------- 路徑


def main_checkout_root(start: Optional[Path] = None) -> Path:
    """解析 main checkout 根目錄——主 checkout 與其所有 worktree 皆回同一路徑。

    ``git rev-parse --git-common-dir`` 在主 checkout 回 ``.git``(相對),在 worktree
    內回主 checkout 的 ``.git`` 絕對路徑;其父目錄即 main checkout 根。

    這個「所有 worktree 收斂到同一個錨點」的性質正是本工作流需要的:指揮站可能坐在
    任何一個 worktree 裡,而 mission 目錄必須是同一個,否則同一個 mission 會因為你
    當下站在哪裡而讀到不同的檔案。

    任何解析失敗(非 git 目錄、git 不可用)SHALL 退回 `start`,never raise——退化後果
    僅為 mission 目錄落在當前目錄,不影響正確性以外的行為。
    """
    base = Path(start) if start else Path.cwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return base
    if result.returncode != 0 or not result.stdout.strip():
        return base
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (base / common).resolve()
    return common.parent


def mission_home(repo: Optional[Path] = None) -> Path:
    """mission 目錄的錨點。`SESSION_DISPATCH_HOME` 優先，否則 `<repo>/.claude/missions`。

    **路徑知識只住在本函式。** 呼叫方 SHALL 經此取得,SHALL NOT 自行拼接——散落各處
    的路徑拼接是一種特別難查的故障:遷移時只要漏改一處,那一處會安靜地讀寫一個沒有
    人在看的舊位置,而三條降級分支全部不觸發。
    """
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return main_checkout_root(repo) / ".claude" / "missions"


def mission_path(mission_id: str, repo: Optional[Path] = None) -> Path:
    """plan 檔的 canonical 路徑。"""
    return mission_home(repo) / f"{mission_id}.json"


def brief_path(mission_id: str, node_id: str, repo: Optional[Path] = None) -> Path:
    """派工單的 canonical 路徑。

    派工單落檔而非塞進命令列,有三個理由:避開把數百字塞進 shell argv 的引號地獄、
    使派工單成為可審閱可重跑的 durable artifact、以及讓「訊息只帶指針」這條原則在
    dispatch 這一段也成立(少一條要記的例外)。
    """
    return mission_home(repo) / mission_id / f"{node_id}.brief.md"


def worker_output_dir(node: "MissionNode", container_name: Optional[str] = None) -> Path:
    """worker 產物該落的目錄——**worker 自己的容器**，不是 mission 目錄。

    ## 為什麼產物不落 mission 目錄

    被要求寫到 mission 目錄絕對路徑的 worker,若使用檔案編輯工具,會被 guard 擋下並改寫到
    **自己容器內的同名相對路徑**——訊息回報「做完了」,而指揮站的 `done_signal` 永遠不會
    fire。所以完成訊號要指向 worker 寫得到的地方。

    ## ⚠️ 這條限制強制不了「plan 只由指揮站寫」

    早期版本的 docstring 寫著它「免費強制」了那條不變式。**那句話比實際保證強**,而一條
    寫在文件裡、比實際強的安全保證,會讓讀者據此決定不必再加防護。實測矩陣:

    | | 路徑 | 結果 |
    |---|---|---|
    | 擋 | 檔案編輯工具寫入所在 repo 的 shared checkout | 拒絕(訊息誘導改寫容器內同名路徑) |
    | 擋 | shell 的 ``git -C <容器之外>``、無法靜態證明留在容器內的複合命令 | 拒絕。**只在 git 或命令形狀上發作,對單純檔案寫入一次都沒發作** |
    | 不擋 | shell 直接檔案寫入 shared checkout | 穿透。**寫入量不是判準**——500 行與複合寫入皆穿透 |
    | 不擋 | Python process 寫入 | 穿透。`write_brief()` / `write_mission()` 正是靠這個縫 |
    | 不擋 | POSIX 檔案權限 | worker 與指揮站同一個 OS user,mission 目錄可寫 |

    準確的說法:**檔案編輯工具會拒絕越界寫入,shell 與 Python 不會。因此「plan 只由指揮站
    寫」是一條慣例,其強度取決於 worker 選了哪個工具——而偏好 shell 的執行模式正把 worker
    推向那一側。要讓它成為不變式,需要工具層以外的機制。** 「多層防護疊加」是同一個錯誤的
    第二個版本,同樣不要寫——那兩道 guard 只在特定命令形狀上發作,不是縱深防禦。

    連帶地,上一段的「worker 會改寫到容器內同名路徑」是**條件成立**:那個轉向由編輯工具的
    拒絕訊息誘導,只在 worker 選了它時發生。選 shell 的 worker 靜默寫穿,此時訊號照常 fire
    而不變式已破——兩條路的失效方向相反。

    ## 兩個合法落點

    1. **worker 容器內的檔案**(本函式)——輕量、免 commit;容器被移除即消失,故指揮站
       SHALL 在收尾前把要保留的內容讀走。
    2. **worker 分支上的 commit**——`done_signal` 寫成
       ``git log --oneline <branch> | grep -q <marker>``;產物活過容器清理,代價是
       worker 得動 git。

    需要留存的產物走 (2),一次性回報走 (1)。

    ## 為什麼收 node 而不是收 `(worktree_name, repo)`

    舊簽名有兩個坑。其一,`repo` 在本模組是**過載**的:本函式要的是**目標 repo**,而
    `mission_path()` / `brief_path()` 要的是**指揮站 repo**(plan 與派工單必須留在指揮站)。
    同名不同義的參數是最難查的那種 bug,因為呼叫點看起來完全正確。其二,預設值只對同
    repo 成立——它在單 repo 用法下不會發作,但會在第一次真的接上跨 repo dispatch 時發作。

    新簽名讓**前提成為簽名的一部分**:同 repo 節點的容器由指揮站以啟動旗標建立並命名,
    故預設取 `node.session_name`;跨 repo 節點的容器多半由目標 repo 自己的工作流建立
    (實測:一個命名為 `xrepo-smoke-probe` 的 session,自行隔離後容器叫 `xrepo-smoke`),
    指揮站推不準,故 `container_name` **必填**,未給就炸。

    回一條猜的路徑,失效形態是永遠 pending;在呼叫點就炸,至少看得見。
    """
    workspace = node.workspace_repo.strip()
    name = (container_name or "").strip()
    if not name:
        if workspace:
            raise ValueError(
                f"節點 {node.id!r} 宣告了 workspace_repo，其容器名不在指揮站的控制內"
                "（多半由目標 repo 自己的工作流建立並命名），故 container_name 必填。"
                "若指揮站並未親自建立該容器，正確做法是改用 commit 形態的 done_signal"
                "（`git log --oneline <指揮站指定的分支> | grep -q <marker>`），"
                "而不是猜一條路徑。"
            )
        name = node.session_name.strip()
        if not name:
            raise ValueError(f"節點 {node.id!r} 無 session_name，無法推導容器名。")
    root = main_checkout_root(Path(workspace) if workspace else None)
    return root / ".claude" / "worktrees" / name


# --------------------------------------------------------------------------- 資料模型


@dataclass(frozen=True)
class MissionNode:
    """一個可派工的工作單元。

    `done_signal` 是本節點完成與否的**唯一權威**：一條 shell 指令,退出碼 0 即完成。
    無法宣告它的節點 SHALL NOT 被派工——那代表任務定義尚未收斂。

    `base_ref` 對 `session` 形態必填。以隔離 worktree 開出的 worker,其 base 往往是
    **啟動方當下的 HEAD** 而非主線;指揮站通常正坐在自己的工作分支上,於是 worker 會
    隱式繼承那些 commits。繼承本身不被禁止——有時正是想要的——但它 SHALL 被顯式選擇
    而非默默發生。欄位雖有空字串預設,漏填仍在建構期就炸,決定藏不回去。

    `shape` 預設 `session`,**預設落在約束較嚴格的一側**:漏填得到的是完整約束,不是
    豁免。`subagent` 形態豁免 `done_signal` 與 `base_ref`——前者是為了隔著通道判定他方
    狀態而存在,後者需要一個獨立 worktree,兩個前提 subagent 都沒有。把它們套上去只會
    製造儀式,不產生保證。

    `workspace_repo` 是 worker 的 workspace 所在 git repository,**空字串＝指揮站自己
    的 repo**。空預設讓導入前寫成的 plan 逐字不變地繼續運作。非空時:

    - **SHALL 為絕對路徑**,建構期驗證。相對路徑的基準是 process cwd,而 cwd 在
      long-running agent session 中會跨 tool call 改變——同一個宣告會在不同時刻指向
      不同 repo,而三者外觀相同。
    - **存在性刻意不在此驗證。** 「路徑寫錯」是指揮站的 bug(該立刻炸),「路徑消失」
      是環境變了(該被看見,但不該讓整份 plan 讀不進來)。後者由 `evaluate_signal()`
      以 `unavailable` 表達——兩種不同的錯不共用一個出口。
    - `base_ref` 隨之解讀為**該 repo 內**的 ref,不是指揮站 repo 的 ref。
    - 其 `done_signal` SHALL 為 git artifact 形態(見 `worker_output_dir()` 的拒答理由)。

    `work_branch` 是**指揮站指定給 worker 的工作分支名**,空字串＝未指定。它是收尾階段
    定位容器的錨點(見 `plan_worker_teardown()`),存在理由是**分支名不能從 `done_signal`
    反解**:那是一條自由格式 shell 指令,``git log --oneline <branch> | grep -q <marker>``
    只是最常見的一種寫法,以正則從中挖分支名會在第一個換寫法的節點上失敗,而失敗形態是
    「定位到 None」——與「容器真的不在了」同形。錨點要能被信任就 SHALL 被顯式宣告,不能
    靠猜。
    """

    id: str
    session_name: str
    brief_path: str
    done_signal: str = ""
    base_ref: str = ""
    model: str = ""
    depends_on: tuple[str, ...] = ()
    needs_exclusive_checkout: bool = False
    status: str = "pending"
    shape: str = "session"
    workspace_repo: str = ""
    work_branch: str = ""

    def __post_init__(self) -> None:
        if self.status not in NODE_STATUSES:
            raise ValueError(f"未知的節點狀態：{self.status!r}（合法值：{NODE_STATUSES}）")
        if self.shape not in NODE_SHAPES:
            raise ValueError(f"未知的節點形態：{self.shape!r}（合法值：{NODE_SHAPES}）")
        if not self.brief_path.strip():
            raise ValueError(
                f"節點 {self.id!r} 缺 brief_path。派工單落檔是跨形態義務——"
                "subagent 節點同樣 SHALL 有一份可審閱、可重跑的派工單。"
            )
        ws = self.workspace_repo.strip()
        if ws and not Path(ws).is_absolute():
            raise ValueError(
                f"節點 {self.id!r} 的 workspace_repo 需為絕對路徑，得到 {self.workspace_repo!r}。"
                "相對路徑的解析基準是 process cwd，而 cwd 在 long-running agent session 中"
                "會跨 tool call 改變——同一個宣告會在不同時刻指向不同 repo，三者外觀相同。"
            )
        if self.shape != "session":
            return
        if not self.done_signal.strip():
            raise ValueError(
                f"節點 {self.id!r} 缺 done_signal。無法用一行指令描述「怎樣算做完」"
                "代表任務定義尚未收斂，此時該繼續討論而非派工。"
            )
        if not self.base_ref.strip():
            raise ValueError(
                f"節點 {self.id!r} 缺 base_ref。worker worktree 會隱式繼承指揮站的 HEAD，"
                "base 必須被顯式選擇。"
            )


@dataclass(frozen=True)
class Mission:
    """一次 dispatch 的完整作戰圖。"""

    mission_id: str
    created_at: str
    nodes: tuple[MissionNode, ...] = ()
    #: 收尾合成報告的落點；未收尾時為空。
    report_path: str = ""

    def node(self, node_id: str) -> Optional[MissionNode]:
        return next((n for n in self.nodes if n.id == node_id), None)


@dataclass(frozen=True)
class SignalOutcome:
    """單一節點的訊號求值結果。

    `state` 為 `done` / `pending` / `unavailable` 三態之一。**呈現層 SHALL NOT 把
    `unavailable` 併入任一側**：把「指令跑不起來」渲染成「還沒做完」會讓一個壞掉的
    訊號看起來像一個仍在進行的節點,指揮站因此永遠等下去。
    """

    node_id: str
    state: str
    detail: str = ""

    @property
    def is_done(self) -> bool:
        return self.state == "done"


# --------------------------------------------------------------------------- 讀寫


def write_brief(
    mission_id: str, node_id: str, text: str, repo: Optional[Path] = None
) -> Path:
    """把派工單落檔並回傳路徑。**只有指揮站呼叫。**

    存在理由不只是對稱:worktree-isolated 的 session 往往**無法用 agent 的檔案編輯
    工具寫到自己 worktree 之外**(harness guard 擋下),而 mission 目錄錨定在 main
    checkout。指揮站多半正坐在某個 worktree 裡,於是「寫派工單」這個動作必須經由本
    函式(Python 層)完成——guard 擋的是工具呼叫,不是 process。

    把它做成 API 而不是讓呼叫方各自想辦法繞,是為了不讓工作流依賴 guard 的實作細節
    ——guard 的邊界會變,本函式的語意不會。
    """
    path = brief_path(mission_id, node_id, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_mission(mission: Mission, repo: Optional[Path] = None) -> Path:
    """把 plan 落檔（atomic write）。

    **只有指揮站呼叫本函式。** worker SHALL NOT 寫 plan（見 module docstring 不變式 1）。
    """
    path = mission_path(mission.mission_id, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(mission)
    payload["nodes"] = [
        {**asdict(n), "depends_on": list(n.depends_on)} for n in mission.nodes
    ]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def read_mission(mission_id: str, repo: Optional[Path] = None) -> Optional[Mission]:
    """讀 plan；檔案不存在或無法解析回 ``None``。

    回 ``None`` 的兩種成因(不存在 / 壞掉)在此**刻意不分**:plan 檔是本工作流自己寫的,
    壞掉屬於程式錯誤而非外部能力差異,呼叫方的正確反應同為「這個 mission 讀不到,停
    下來看」。這與外部能力偵測(見 `live_sessions` 的 `available` 旗標)是不同的判斷,
    別把兩者的處理方式互相套用。
    """
    path = mission_path(mission_id, repo)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        nodes = tuple(
            MissionNode(**{**n, "depends_on": tuple(n.get("depends_on", ()))})
            for n in raw.get("nodes", [])
        )
        return Mission(
            mission_id=str(raw["mission_id"]),
            created_at=str(raw.get("created_at", "")),
            nodes=nodes,
            report_path=str(raw.get("report_path", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def set_node_status(
    mission: Mission, node_id: str, status: str, repo: Optional[Path] = None
) -> Mission:
    """更新單一節點狀態並落檔，回傳新的 Mission（frozen dataclass，不就地改）。"""
    if status not in NODE_STATUSES:
        raise ValueError(f"未知的節點狀態：{status!r}（合法值：{NODE_STATUSES}）")
    if mission.node(node_id) is None:
        raise KeyError(f"mission {mission.mission_id!r} 內無節點 {node_id!r}")
    updated = replace(
        mission,
        nodes=tuple(
            replace(n, status=status) if n.id == node_id else n for n in mission.nodes
        ),
    )
    write_mission(updated, repo)
    return updated


# --------------------------------------------------------------------------- dispatch


@dataclass(frozen=True)
class DispatchPlan:
    """dispatch 一個節點所需的 deterministic 事實。**plan-only：不開 session、不建容器。**

    存在理由是**沒有消費者的欄位會死**。本模組裡沒有 launcher——dispatch 整步是散文流程,
    於是「plan 宣告了 workspace_repo」與「launch 指令真的切了 cwd、真的授權了附加目錄」之間
    沒有任何連結。一份宣告正確的 plan 配一條忘了切 cwd 的指令,得到的是「worker 落在指揮站
    repo,而訊號忠實地在目標 repo 求值」——永遠 pending,且兩邊各自看起來都對。本 dataclass
    把那條連結變成可計算的東西。

    **刻意不含 CLI 指令字串。** 旗標形狀是外部工具的非正式契約(會隨版本漂移),其 SSOT 是
    `SKILL.md`;複製進本層會讓同一件事有兩個真相層,而兩者會各自演化。本層回答的是**repo
    推導得出的事實**,渲染成指令由上層負責。

    `container_from_worker` 為真時,指揮站 SHALL NOT 推導容器路徑(見 `worker_output_dir()`),
    且派工單 SHALL 把進入容器寫為 worker 的第一個動作——**背景 session 不會被自動隔離**,
    兩個 repo 皆然,且目標 repo 的隔離設定更積極也不改變這件事。指揮站 repo 用起來像是自動
    的,是因為它的工作流層會顯式建容器;目標 repo 沒有這一層。
    """

    node_id: str
    #: launch 指令該從哪個目錄下。
    cwd: Path
    #: 需顯式授權給 worker 的附加目錄。跨 repo 才非空——worker 讀不到指揮站的派工單就
    #: 無法開工,而該授權同時擴大它對指揮站 repo 的可及範圍,故 SHALL 出現在確認畫面上。
    add_dirs: tuple[Path, ...]
    #: 派工單絕對路徑。**恆在指揮站 repo**,不因 worker 在別的 repo 而搬家。
    brief_abs: Path
    #: 指揮站要建立的容器名；由 worker 自建時為空字串。
    container_name: str
    container_from_worker: bool


def dispatch_plan(
    node: MissionNode,
    mission_id: str,
    repo: Optional[Path] = None,
    container_name: Optional[str] = None,
) -> DispatchPlan:
    """自節點推導 dispatch 所需的事實。**純函式：只讀檔案系統與節點，無任何副作用。**

    `repo` 是**指揮站的** repo(派工單錨點),不是目標 repo——目標 repo 一律取自
    `node.workspace_repo`。這兩個錨點在跨 repo 節點下必然不同,把它們壓成同一個參數正是
    舊 `worker_output_dir(worktree_name, repo)` 的坑。

    容器歸屬有兩條路,對應「隔離 SHALL 被顯式安排」的析取:

    - **指揮站建容器**——`container_name` 有值(同 repo 節點預設取 `node.session_name`)。
    - **worker 自己進容器**——跨 repo 節點且未指定 `container_name`。此時派工單 SHALL 把
      進入容器寫為第一個動作。

    跨 repo 節點的預設落在第二條,是因為既有條文已規定「派工單指定既有分支時 SHALL 省略
    容器旗標」(目標分支被別的 worktree 持有時開不出來),而跨 repo 續做既有分支的情境正是
    那個形狀。但兩條路都可表達——選哪條是指揮站的決定,不是工具的預設。
    """
    workspace = node.workspace_repo.strip()
    brief = brief_path(mission_id, node.id, repo)
    station_root = main_checkout_root(repo)
    name = (container_name or "").strip()
    if workspace:
        return DispatchPlan(
            node_id=node.id,
            cwd=Path(workspace),
            add_dirs=(brief.parent,),
            brief_abs=brief,
            container_name=name,
            container_from_worker=not name,
        )
    return DispatchPlan(
        node_id=node.id,
        cwd=station_root,
        add_dirs=(),
        brief_abs=brief,
        container_name=name or node.session_name.strip(),
        container_from_worker=False,
    )


# --------------------------------------------------------------------------- 收尾


@dataclass(frozen=True)
class WorkerTeardownPlan:
    """單一 worker 容器的收尾狀態與建議步驟。**plan-only：不拆任何東西。**

    ## 它回答容器，不回答政策

    `state` 說的是「這個節點有沒有容器還佔在目標 repo 裡」,**不是**「這個節點的分支該不該
    刪、該不該 merge」。後者是目標 repo 自己工作流的範疇,而指揮站對它一無所知——依指揮站
    的分支慣例去判定一條外來分支,產生的建議不會報錯,只會錯。

    所以 `steps` 只含容器與 session 的處置動作;分支去留以 `branch_merged` 作為**事實**
    呈現,SHALL NOT 被渲染成本 mission 的待辦。

    ## 三態的判準是「有沒有可信錨點」,不是「有沒有找到」

    `disposed` 是一句**正面斷言**:「以可信錨點枚舉過目標 repo 的 worktree,無一屬於本節點」。
    缺錨點(未宣告 `work_branch`)或 repo 不可達時,枚舉同樣全部落空,**外觀與已處置完全
    相同**——此時回 `disposed` 等於用「我沒找到」冒充「它不存在」,故回 `unavailable`。

    `notes` 收「要被看見但不改變 `state`」的觀察:兩路定位不一致、容器即目標 repo 主
    checkout(worker 從未被隔離)等。它們不是錯誤,但沉默地放過去就等於沒發生。
    """

    node_id: str
    state: str
    reason: Optional[str] = None
    worktree_path: Optional[str] = None
    dirty_files: list[str] = field(default_factory=list)
    blocking_sessions: list[str] = field(default_factory=list)
    unpushed: bool = False
    branch_merged: Optional[bool] = None
    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_disposed(self) -> bool:
        return self.state == "disposed"


def _teardown_git(repo: str, *args: str) -> tuple[int, str]:
    """在目標 repo 內跑 git 讀指令，never raise，回 ``(rc, stdout)``。

    一律顯式帶 ``-C <repo>``:本函式的呼叫點恆為外部 repo,依賴 process cwd 會在 cwd 中途
    改變時安靜地問錯 repo。
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=_SIGNAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return result.returncode, result.stdout


def _branch_worktree_path(branch: str, repo: str) -> Optional[str]:
    """`branch` 被任一 worktree（含 repo 自身）checkout 時的絕對路徑，否則 ``None``。

    解析 ``git worktree list --porcelain``,而非自己拼 ``.claude/worktrees/<name>``——
    容器名推不準(見 `worker_output_dir()`),而分支反查問的是 git 自己記的帳。
    """
    rc, out = _teardown_git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    current_path: Optional[str] = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :].strip()
        elif line.startswith("branch ") and current_path:
            ref = line[len("branch ") :].strip()
            name = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            if name == branch:
                return current_path
            current_path = None
    return None


def _dirty_files(repo: str) -> list[str]:
    """`repo` 的未 commit 變更（**含 untracked**）相對路徑清單；乾淨或 git 不可用回空清單。

    ``git status --porcelain`` 每行為 ``XY <path>``(第 4 字元起為路徑);rename 形如
    ``R  old -> new``,取箭頭後的新路徑。
    """
    rc, out = _teardown_git(repo, "status", "--porcelain")
    if rc != 0:
        return []
    files: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path.strip('"'))
    return files


def _session_cwd(session_name: str) -> Optional[str]:
    """roster 中該具名 session 的 cwd；roster 不可用或查無此名回 ``None``。

    **lazy import**:`live_sessions` 是本 module 唯一依賴外部 CLI 的下游,module 層 import
    會讓一個純 JSON/subprocess 的 module 在 import 時就綁上 `claude` binary 的可用性。
    """
    try:
        from session_dispatch.live_sessions import (  # noqa: PLC0415 - 見 docstring
            enumerate_live_sessions,
        )

        roster = enumerate_live_sessions()
        if not roster.available:
            return None
        for session in roster.sessions:
            if session.name == session_name:
                return session.cwd
    except Exception:  # noqa: BLE001 - best-effort：輔路徑故障不得阻塞收尾判定
        return None
    return None


def _holding_sessions(worktree_path: str, repo: str) -> list[str]:
    """持有該容器的 live session 一行式描述；查不到或故障回空清單。"""
    try:
        from session_dispatch.live_sessions import (  # noqa: PLC0415 - 同 `_session_cwd`
            sessions_holding,
        )

        return [s.describe() for s in sessions_holding(worktree_path, repo=Path(repo))]
    except Exception:  # noqa: BLE001 - best-effort
        return []


def plan_worker_teardown(
    node: MissionNode, repo: Optional[Path] = None
) -> WorkerTeardownPlan:
    """自節點推導其 worker 容器的處置狀態與收尾步驟。

    **plan-only、never raise**：只跑 git 讀操作與 roster 枚舉，SHALL NOT unlock / remove /
    branch / commit / push，SHALL NOT 刪除任何 session。與 `dispatch_plan()` 同一紀律。

    `repo` 是**指揮站的** repo，只在節點未宣告 `workspace_repo` 時作為錨點；跨 repo 節點
    一律以 `node.workspace_repo` 為準。

    ## 定位:主路徑是分支,不是容器名

    容器名**推不準**——指揮站命名 `xrepo-smoke-probe` 的 session,worker 自行隔離後容器叫
    `xrepo-smoke`;走目標 repo 自己工作流的 worker 更由那個工作流命名(這正是
    `worker_output_dir()` 對跨 repo 拒答的理由)。但收尾要的是**容器路徑**,而那可以用
    `node.work_branch` 於目標 repo 內反查取得。

    主路徑選分支而非 roster,是因為**收尾的典型時機正是 session 已經停掉之後**——roster 那
    時已經查不到,以它為主路徑等於在最需要的時刻失效。roster 降為輔路徑,做兩件事:補
    `blocking_sessions`,以及在分支反查未命中時提供「這個 session 其實坐在哪」的交叉觀察。

    ## 為什麼多節點共用同一個 repo 時不合併可達性檢查

    同一 `workspace_repo` 的 N 個節點會各跑一次 ``git rev-parse``。這是純效能問題,不影響
    正確性,而合併需要一個跨節點的快取層——那會讓本函式從「吃一個節點」變成「吃一份
    mission 的狀態」,為了幾次 subprocess 換掉單節點可測性,不划算。
    """
    workspace = node.workspace_repo.strip()
    branch = node.work_branch.strip()
    notes: list[str] = []

    if workspace:
        reason = _repo_unreachable_reason(workspace)
        if reason is not None:
            return WorkerTeardownPlan(node.id, "unavailable", reason=reason)
        target_repo = workspace
    else:
        target_repo = str(main_checkout_root(repo))

    if not branch:
        return WorkerTeardownPlan(
            node.id,
            "unavailable",
            reason=(
                f"節點 {node.id!r} 未宣告 work_branch，無可信定位錨點。"
                "缺錨點時 worktree 枚舉同樣全部落空，外觀與「已處置」完全相同——"
                "回 disposed 等於用「我沒找到」冒充「它不存在」。"
            ),
        )

    by_branch = _branch_worktree_path(branch, target_repo)
    by_session = _session_cwd(node.session_name.strip()) if node.session_name.strip() else None

    worktree_path = by_branch
    if by_branch is None and by_session and _is_within(by_session, target_repo):
        worktree_path = by_session
        notes.append(
            f"分支 {branch!r} 未被任何 worktree 持有，但 session "
            f"{node.session_name!r} 的 cwd 落在 {by_session}——"
            "worker 可能改用了他名分支，或在容器外直接工作。"
        )
    elif by_branch and by_session and not _is_within(by_session, by_branch):
        notes.append(
            f"兩路定位不一致：分支反查得 {by_branch}，而 session "
            f"{node.session_name!r} 的 cwd 為 {by_session}。"
        )

    if worktree_path is None:
        return WorkerTeardownPlan(
            node.id,
            "disposed",
            branch_merged=_branch_merged(target_repo, branch, node.base_ref.strip()),
            notes=notes,
        )

    repo_main = _repo_main_checkout(target_repo)
    is_main = (
        repo_main is not None
        and Path(worktree_path).resolve() == Path(repo_main).resolve()
    )
    if is_main:
        notes.append(
            f"定位到的路徑即目標 repo 的主 checkout（{worktree_path}）——"
            "該 worker 的隔離從未被安排。這不是可拆的容器，"
            "產物落地與否要另外確認（背景 session 不會自動隔離，兩個 repo 皆然）。"
        )

    blocking = _holding_sessions(worktree_path, target_repo)
    return WorkerTeardownPlan(
        node.id,
        "pending",
        worktree_path=worktree_path,
        dirty_files=_dirty_files(worktree_path),
        blocking_sessions=blocking,
        unpushed=_has_unpushed(target_repo, branch),
        branch_merged=_branch_merged(target_repo, branch, node.base_ref.strip()),
        steps=_teardown_steps(
            repo=target_repo,
            worktree_path=worktree_path,
            session_name=node.session_name.strip(),
            blocked=bool(blocking),
            is_main_checkout=is_main,
        ),
        notes=notes,
    )


def _is_within(child: str, parent: str) -> bool:
    """`child` 是否落在 `parent` 子樹內（含相等）；無法 resolve 回 ``False``。"""
    try:
        return Path(child).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, ValueError):
        return False


def _repo_main_checkout(repo: str) -> Optional[str]:
    """目標 repo 的主 checkout 路徑（``git worktree list --porcelain`` 第一條）。"""
    rc, out = _teardown_git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    return None


def _has_unpushed(repo: str, branch: str) -> bool:
    """`branch` 上是否有從未 push 到任何 remote 的 commit。

    這是容器清理被拒絕的**第二種**條件,且在 squash 流程下結構上必然發生——內容早已進
    主線,只是那些 SHA 沒被 push 過。判定不了時回 ``False``(誤差方向為少提醒一次,而非
    憑空宣稱有未 push 的東西)。
    """
    rc, out = _teardown_git(repo, "rev-list", "--count", branch, "--not", "--remotes")
    if rc != 0:
        return False
    return out.strip().isdigit() and int(out.strip()) > 0


def _branch_merged(repo: str, branch: str, base_ref: str) -> Optional[bool]:
    """`branch` 是否已併入 `base_ref`；任一 ref 解析不了回 ``None``（三態，不用 False 代表未知）。

    這是**事實不是待辦**:分支在目標 repo 內何時 merge、何時刪除,是那個 repo 工作流的
    決定。本欄位存在是為了讓收尾的人看得到全貌,SHALL NOT 被渲染成本 mission 的未完成項。
    """
    if not base_ref:
        return None
    rc, _ = _teardown_git(repo, "merge-base", "--is-ancestor", branch, base_ref)
    if rc == 0:
        return True
    return False if rc == 1 else None


def _teardown_steps(
    *,
    repo: str,
    worktree_path: str,
    session_name: str,
    blocked: bool,
    is_main_checkout: bool,
) -> list[str]:
    """組收尾步驟。**只含容器與 session 的處置，不含目標 repo 的分支政策。**

    ## 順序 invariant（SHALL NOT 為便利而調整）

    ``worktree unlock`` 一律排在 ``worktree remove`` 之前。這是實測而非偏好:session 停掉
    之後容器的 lock **仍在**,少了 unlock 這一步,清單的第一個指令就會失敗。該關係由單元
    測試以索引大小關係釘住——寫在註解裡的順序約束會在第一次重排時安靜失效。

    ## 為什麼沒有 ``git branch -D``

    分支去留是目標 repo 的政策。依指揮站的分支慣例去判定一條外來分支,據此產出的刪除建議
    不會報錯、只會錯。

    ## session 刪除的地位

    它處置的是 **session**,不是容器。列進清單是因為 worker session 不刪會累積,但**容器
    處置的判定不以它成敗為準**——把兩者綁在一起會讓「session 已刪但容器還在」變成一個
    不可表達的狀態,而那正是最該被看見的那個。

    每一項都顯式帶 ``git -C <repo>``:由人執行時,指揮站自身多半坐在自己的容器裡,未顯式
    定位的 git 指令會被 guard 攔下或作用在錯誤的 repo 上。
    """
    steps: list[str] = []
    if not is_main_checkout and not blocked:
        steps.append(f"git -C {repo} worktree unlock {worktree_path}")
        steps.append(f"git -C {repo} worktree remove {worktree_path}")
    if session_name:
        steps.append(f"claude rm {session_name}")
    return steps


# --------------------------------------------------------------------------- 訊號求值


def _repo_unreachable_reason(path: str) -> Optional[str]:
    """`workspace_repo` 不可用時回一句原因，可用時回 ``None``。

    以 ``git -C <path> rev-parse --git-dir`` 判定,而非自己檢查 ``.git`` 的存在——
    worktree 的 ``.git`` 是**檔案**不是目錄,手刻佈局判斷會把一個完全正常的 worktree
    判成非 repo。同一次呼叫也一併涵蓋「路徑根本不在」。
    """
    target = Path(path)
    if not target.exists():
        return f"workspace_repo 不存在：{path}"
    if not target.is_dir():
        return f"workspace_repo 不是目錄：{path}"
    try:
        rc = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=_SIGNAL_TIMEOUT_SECONDS,
        ).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"workspace_repo 無法判定（{exc}）：{path}"
    return None if rc == 0 else f"workspace_repo 不是 git repository：{path}"


def evaluate_signal(node: MissionNode, cwd: Optional[Path] = None) -> SignalOutcome:
    """執行節點的 `done_signal`，以三態回報。

    退出碼 0 → ``done``；非零 → ``pending``；**指令本身跑不起來**（逾時、OSError、
    退出碼 126/127）→ ``unavailable`` 並附原因。第三態是本函式存在的主要理由:一條壞掉
    的訊號指令若被渲染成「還沒做完」,指揮站會永遠等一個不會到來的完成。

    ## 求值錨點

    節點宣告 `workspace_repo` 時**以它為 cwd**,否則沿用傳入的 `cwd`。錨點下沉到節點是
    跨 repo mission 的正確性前提:一份 mission 可以同時含本 repo 與跨 repo 節點,以單一
    cwd 求值全部節點必然錯一半。

    宣告的 repo 不可達時回 ``unavailable``。理由是最常見的跨 repo 訊號形態
    ``git log --oneline <branch> | grep -q <marker>`` 在錯誤的 repo 內會吐 usage 錯誤並
    回非零——**於是一個永遠不會 fire 的訊號長得跟一個還在工作的 worker 一模一樣**。
    錨點錯誤是能力故障,不是進度狀態。

    ## 這個三態辨識得到什麼、辨識不到什麼

    **辨識不到「指令跑起來但壞了」。** 一條因參數寫錯而回退出碼 1 的指令,與「檢查為否」
    在回傳值上完全同形。本函式只升級兩個語意無歧義的碼:127(找不到指令)、126(找到了但
    不可執行)。git 的 128/129 刻意**不**升級——它們同時用於「不是有效物件」,把它們歸
    `unavailable` 會把「分支還沒出現」誤報成壞掉的訊號,那是第三態的反向誤用。

    所以本函式 SHALL NOT 被描述為「能辨識壞掉的訊號」。它辨識的是**跑不起來**的訊號,
    而跨 repo 的主要失效由上面的 repo 可達性預檢處理,不靠猜退出碼。

    **空訊號 SHALL NOT 被求值。** `subprocess` 對空字串命令回退出碼 0,若照常求值,
    一個沒有 `done_signal` 的節點會被判成「已完成」——最糟的失效方向。故此處先擋,
    並回 `pending` 而非 `unavailable`:訊號不存在是形態的正常結果(subagent 節點),
    不是壞掉的訊號。
    """
    if not node.done_signal.strip():
        return SignalOutcome(
            node.id, "pending", f"節點未宣告 done_signal（shape={node.shape}）"
        )
    workspace = node.workspace_repo.strip()
    if workspace:
        reason = _repo_unreachable_reason(workspace)
        if reason is not None:
            return SignalOutcome(node.id, "unavailable", reason)
        run_cwd: Optional[str] = workspace
    else:
        run_cwd = str(cwd) if cwd else None
    try:
        result = subprocess.run(
            node.done_signal,
            shell=True,
            cwd=run_cwd,
            capture_output=True,
            text=True,
            timeout=_SIGNAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return SignalOutcome(
            node.id, "unavailable", f"done_signal 逾時（>{_SIGNAL_TIMEOUT_SECONDS}s）"
        )
    except OSError as exc:
        return SignalOutcome(node.id, "unavailable", f"done_signal 無法執行：{exc}")
    if result.returncode == 0:
        return SignalOutcome(node.id, "done")
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else f"exit {result.returncode}"
    if result.returncode in _UNRUNNABLE_EXIT_CODES:
        return SignalOutcome(
            node.id,
            "unavailable",
            f"done_signal 跑不起來（exit {result.returncode}）：{detail}",
        )
    return SignalOutcome(node.id, "pending", detail)


def evaluate_all(
    mission: Mission, cwd: Optional[Path] = None
) -> dict[str, SignalOutcome]:
    """對所有非終止態節點求值；已 `done` / `abandoned` 者不重跑。

    `cwd` 是**退路而非全域錨點**:宣告了 `workspace_repo` 的節點各自於其 repo 內求值,
    只有未宣告的節點才用這裡傳入的值。一份 mission 可以同時含本 repo 與跨 repo 節點,
    以單一 cwd 求值全部節點必然錯一半(見 `evaluate_signal()` 的求值錨點段)。

    `subagent` 節點不求值:它沒有 `done_signal`,完成判定取 plan status。這不是降級——
    subagent 的結果直接回到指揮站的 context,不存在「隔著通道判定他方狀態」的問題,
    而 `done_signal` 正是為那個問題存在的。**但它們 SHALL NOT 被列為 `unavailable`**,
    否則呈現層會把一個運作正常的節點渲染成壞掉的訊號。
    """
    out: dict[str, SignalOutcome] = {}
    for n in mission.nodes:
        if n.status == "done":
            out[n.id] = SignalOutcome(n.id, "done")
        elif n.status == "abandoned":
            out[n.id] = SignalOutcome(n.id, "pending", "已放棄")
        elif n.shape == "subagent":
            out[n.id] = SignalOutcome(
                n.id, "pending", "subagent 節點：完成判定取 plan status，不求值訊號"
            )
        else:
            out[n.id] = evaluate_signal(n, cwd)
    return out


def ready_nodes(
    mission: Mission, outcomes: Optional[dict[str, SignalOutcome]] = None
) -> list[MissionNode]:
    """回傳可以開跑的節點：尚未派工、且所有 `depends_on` 皆已完成。

    完成與否取 `outcomes`(訊號求值結果)優先,無 outcomes 時退回節點自身的 `status`
    ——**訊號是權威,plan 的 status 只是指揮站的紀錄**。兩者不一致時以訊號為準,這正是
    「worker 宣稱完成但訊號未出現」該被擋下的地方。

    需要獨佔主 checkout 的節點**每個 workspace repo 至多回傳一個**:那類節點會互相搶同
    一個資源,同組同時派出兩個是製造互等。閘門以 workspace repo 分組而非全域,是因為
    `needs_exclusive_checkout` 的語意是「需要**其 workspace repo 的**主 checkout」——不同
    repo 的主 checkout 是彼此獨立的資源,其鎖不互相排斥,當成單一資源會把跨 repo 的節點
    無謂地循序化。
    """
    done_ids = set()
    for n in mission.nodes:
        if outcomes is not None:
            if outcomes.get(n.id, SignalOutcome(n.id, "pending")).is_done:
                done_ids.add(n.id)
        elif n.status == "done":
            done_ids.add(n.id)

    ready = [
        n
        for n in mission.nodes
        if n.status == "pending" and n.id not in done_ids and set(n.depends_on) <= done_ids
    ]

    seen_exclusive: set[str] = set()
    gated: list[MissionNode] = []
    for n in ready:
        if n.needs_exclusive_checkout:
            if n.workspace_repo in seen_exclusive:
                continue
            seen_exclusive.add(n.workspace_repo)
        gated.append(n)
    return gated


def unavailable_signals(outcomes: dict[str, SignalOutcome]) -> list[SignalOutcome]:
    """挑出求值失敗的節點，供呈現層單獨列出（SHALL NOT 併入「未完成」）。"""
    return [o for o in outcomes.values() if o.state == "unavailable"]


# --------------------------------------------------------------------------- 派工單 lint


#: 段落標記的第二種語法:整行形如 ``—— 標題 ——``。
#:
#: 只認全形破折號,且要求**前後皆有標記、該行無其他內容**——一般散文的破折號出現在句中
#: 而非行首,不會產生此形狀;`---` 這類 markdown 分隔線因標題為空亦不匹配。
_DASH_SECTION_RE = re.compile(r"^\s*—{2,}\s*([^—]+?)\s*—{2,}\s*$")


def lint_brief(text: str) -> list[str]:
    """檢查派工單是否具備五元件的**可觀察證據**；回傳 findings，空 list 即通過。

    ## 這個 lint 抓得到什麼、抓不到什麼

    抓得到:**結構性遺漏**——五個元件任一段整段缺席、有驗證段但沒有驗證指令、線索段
    沒標示「不是指令」、回報項沒寫用途、缺邊界段、缺完成訊號段。

    「整段缺席」與「段落存在但內容缺項」給**不同的 finding 文字**,因為使用者的下一步
    動作不同(前者新增段落,後者補內容)。早期版本把元件 1／2 的檢查寫成
    ``if section is not None and ...``,於是**整段忘了寫時一聲不吭**,恰好放過最該被擋
    的形態,與本函式宣稱的保證相反。

    抓**不**到:內容空洞。一個寫著「（我要拿去參考）」的回報項會通過本 lint,但它跟沒寫
    用途一樣沒用。**語意品質仍是人的判斷**——本 lint 提供的是下限不是保證:聚合綠燈只
    驗證「有沒有」,不驗證「對不對」。

    所以本函式 SHALL NOT 被當成派工單品質的充分條件。它存在的價值是把最常見的失效
    形態(整段忘了寫)從「靠自律」變成「會紅燈」,而不是取代 SKILL.md 的檢查表。

    段落標記語法不影響判定,見 `_section_title()`。
    """
    findings: list[str] = []
    sections = _split_sections(text)

    premise = _find_section(sections, ("已查證", "前提"))
    if premise is None:
        findings.append(
            "元件 1：缺「已查證的前提」段——要下游採信的斷言需逐條列出並各附一條複查"
            "指令；本次若確實沒有預查證的前提，仍需顯式寫明，別留白讓下游自己猜"
        )
    elif "驗：" not in premise and "驗:" not in premise:
        findings.append(
            "元件 1：有「已查證的前提」段但無驗證動作（每條斷言需附一條複查指令，"
            "格式如 `驗：<command>`）——沒有驗證路徑的前提，下游只能整段盲信或整段重查"
        )

    clue = _find_section(sections, ("線索",))
    if clue is None:
        findings.append(
            "元件 2：缺「線索」段——開放性問句與掃描範圍需與指令分開標示；沒有線索要"
            "給時亦需寫明，否則下游無從判斷哪些方向可以自由探索"
        )
    elif "不是指令" not in clue:
        findings.append(
            "元件 2：有「線索」段但未標示「不是指令」——agent 對疑問句與祈使句的區辨"
            "不穩，顯式標示比措辭修飾有效得多"
        )

    if _find_section(sections, ("邊界",)) is None:
        findings.append("元件 4：缺「邊界」段——需寫明並行 session、分支佔用、不可動的檔案")

    if _find_section(sections, ("完成訊號",)) is None:
        findings.append("元件：缺「完成訊號」段——下游需知道做到什麼程度算完成")

    report = _find_section(sections, ("回報",))
    if report is None:
        findings.append("元件 5：缺「回報」段")
    else:
        for item in _numbered_items(report):
            # 用途說明常因換行而跨行，故判定必須以**條目區塊**為單位。逐行比對會把
            # 一份正確的派工單判成缺用途——實測真實派工單各中 2 次。
            if not re.search(r"[（(][^)）]{2,}[)）]", item):
                head = item.splitlines()[0].strip()
                findings.append(
                    f"元件 5：回報項未附用途 → {head[:40]}…（知道用途，下游才知道該答到多細）"
                )

    return findings


def _section_title(line: str) -> Optional[str]:
    """辨識段落標記行並回傳標題；非標記行回 ``None``。

    支援 ``## 標題`` 與 ``—— 標題 ——`` 兩種語法,產生等價的 (標題, 內文) 對。

    **lint 對格式寬容、對內容嚴格。** 段落標記屬呈現細節,因使用者採用模板以外的等價
    寫法而報缺段,製造的是假陽性——而假陽性會讓人開始忽略 lint,那比沒有 lint 更糟。
    這條相容性有實據:本工作流的模板原本用破折號標記而 lint 只認 `## `,於是**照著
    模板寫的派工單會被同一份 skill 指定的檢查判成缺三個段**。
    """
    if line.startswith("## "):
        return line[3:].strip()
    match = _DASH_SECTION_RE.match(line)
    return match.group(1).strip() if match else None


def _split_sections(text: str) -> list[tuple[str, str]]:
    """把派工單依段落標記切段，回傳 (標題, 內文) 對。"""
    out: list[tuple[str, str]] = []
    title, buf = "", []
    for line in text.splitlines():
        heading = _section_title(line)
        if heading is not None:
            if title or buf:
                out.append((title, "\n".join(buf)))
            title, buf = heading, []
        else:
            buf.append(line)
    if title or buf:
        out.append((title, "\n".join(buf)))
    return out


def _numbered_items(section: str) -> list[str]:
    """把段落切成編號條目區塊（`1. …` 起，到下一個編號或段落結束為止）。

    以區塊而非單行為單位,是因為條目的說明常跨行——用途寫在括號裡而右括號落在下一行
    是很常見的排版,逐行判定會誤殺。
    """
    items: list[str] = []
    buf: list[str] = []
    for line in section.splitlines():
        if re.match(r"^\s*\d+[.、]\s+\S", line):
            if buf:
                items.append("\n".join(buf))
            buf = [line]
        elif buf:
            if line.strip() and not line.startswith("#"):
                buf.append(line)
            else:
                items.append("\n".join(buf))
                buf = []
    if buf:
        items.append("\n".join(buf))
    return items


def _find_section(
    sections: list[tuple[str, str]], keywords: tuple[str, ...]
) -> Optional[str]:
    """回傳標題含任一關鍵字的段落（**含標題行**）；找不到回 ``None``。

    刻意連標題一起回傳:五元件的標記常寫在標題裡(`## 線索（不是指令）`),只看 body 會
    把一份正確的派工單判成缺標示。「找不到」與「段落是空的」仍是兩回事——前者回
    ``None``,後者回空字串。
    """
    for title, body in sections:
        if any(k in title for k in keywords):
            return f"{title}\n{body}"
    return None
