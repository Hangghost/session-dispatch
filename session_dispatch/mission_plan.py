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

規約 SSOT：``SPEC.md``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
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


def worker_output_dir(worktree_name: str, repo: Optional[Path] = None) -> Path:
    """worker 產物該落的目錄——**worker 自己的 worktree**，不是 mission 目錄。

    ## 為什麼產物不能落 mission 目錄

    worker 若是 worktree-isolated 的 session,harness 通常擋下它對自身 worktree 之外
    的寫入。實測:一個被要求寫到 mission 目錄絕對路徑的 worker,會改寫到**自己
    worktree 內的同名相對路徑**——然後回報「做完了」,而指揮站的 `done_signal` 永遠
    不會 fire。

    這個限制其實是**免費的不變式強制**:mission 目錄由隔離保證只有指揮站寫得進去,
    「plan 只由指揮站寫」因此不必靠自律。代價是完成訊號必須指向 worker 寫得到的地方。

    兩個合法落點,依產物是否需要活過 worktree 清理而定:

    1. **worker worktree 內的檔案**(本函式)——輕量、免 commit;worktree 被移除即消失,
       故指揮站 SHALL 在收尾前把要保留的內容讀走。
    2. **worker 分支上的 commit**——`done_signal` 寫成
       ``git log --oneline <branch> | grep -q <marker>``;產物活過 worktree 清理,
       代價是 worker 得動 git。

    需要留存的產物走 (2),一次性回報走 (1)。
    """
    return main_checkout_root(repo) / ".claude" / "worktrees" / worktree_name


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


# --------------------------------------------------------------------------- 訊號求值


def evaluate_signal(node: MissionNode, cwd: Optional[Path] = None) -> SignalOutcome:
    """執行節點的 `done_signal`，以三態回報。

    退出碼 0 → ``done``；非零 → ``pending``；**指令本身跑不起來**（逾時、OSError）
    → ``unavailable`` 並附原因。第三態是本函式存在的主要理由:一條壞掉的訊號指令若被
    渲染成「還沒做完」,指揮站會永遠等一個不會到來的完成。

    **空訊號 SHALL NOT 被求值。** `subprocess` 對空字串命令回退出碼 0,若照常求值,
    一個沒有 `done_signal` 的節點會被判成「已完成」——最糟的失效方向。故此處先擋,
    並回 `pending` 而非 `unavailable`:訊號不存在是形態的正常結果(subagent 節點),
    不是壞掉的訊號。
    """
    if not node.done_signal.strip():
        return SignalOutcome(
            node.id, "pending", f"節點未宣告 done_signal（shape={node.shape}）"
        )
    try:
        result = subprocess.run(
            node.done_signal,
            shell=True,
            cwd=str(cwd) if cwd else None,
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
    return SignalOutcome(node.id, "pending", detail)


def evaluate_all(
    mission: Mission, cwd: Optional[Path] = None
) -> dict[str, SignalOutcome]:
    """對所有非終止態節點求值；已 `done` / `abandoned` 者不重跑。

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

    需要獨佔主 checkout 的節點至多回傳一個:那類節點會互相搶同一個資源,同時派出兩個
    是製造互等。
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

    seen_exclusive = False
    gated: list[MissionNode] = []
    for n in ready:
        if n.needs_exclusive_checkout:
            if seen_exclusive:
                continue
            seen_exclusive = True
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
