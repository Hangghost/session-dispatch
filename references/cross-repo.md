# 跨 repo dispatch

這份文件是 session-dispatch 的跨 repo dispatch playbook——worker 的 workspace 是**另一個
git repository** 時要讀。驅動情境：指揮站 repo 派工，worker 在某個 code repo 跑該 repo 自己
的工作流。

規約 SSOT 在 `SPEC.md`；本檔是操作面。

---

## 一句話版本

**兩個錨點，別壓成一個。** plan 與派工單恆在指揮站 repo，worker 與訊號求值恆在目標 repo。
`dispatch_plan(node, mission_id)` 就是為了讓你不必手推這兩個錨點而存在。

---

## 節點怎麼宣告

    MissionNode(
        id="n1",
        session_name="mission-n1",
        brief_path="…",                            # 恆在指揮站
        base_ref="main",                           # ← 目標 repo 的 main
        workspace_repo="/abs/path/to/target-repo",  # ← 絕對路徑，建構期驗
        done_signal="git log --oneline feature/x | grep -q '[N1-DONE]'",
    )

`workspace_repo` **必須是絕對路徑**。相對路徑的基準是 process cwd，而 cwd 在背景 session 中
會跨 tool call 改變——同一個宣告會在不同時刻指向不同 repo，而三者外觀相同。建構期就炸。

**存在性不在建構期驗。** 「路徑寫錯」是你的 bug，「路徑消失」是環境變了；後者由求值層以
`unavailable` 出聲，不該讓整份 plan 讀不進來。

---

## 為什麼一定要宣告：錨錯 repo 的訊號長得像「還在做」

`done_signal` 是自由格式 shell，你今天就能寫 `git -C /abs/path log …` 把它指到別的 repo——
**表示得出來**。所以加欄位不是為了整潔，是為了失效模式：

    rel（錨錯 repo）: SignalOutcome(state='pending', detail="'git <command> [<revision>...]…'")
    abs（正確錨定）: SignalOutcome(state='done')

錨錯 repo 的 git 指令**實際失敗了**（吐 usage 錯誤、退出碼非零），卻被判成 `pending`。
於是一個永遠不會 fire 的訊號，跟一個還在工作的 worker 外觀完全相同——指揮站永遠等下去。

宣告 `workspace_repo` 後，求值前會先檢查該 repo 可達（路徑在、是目錄、`git -C … rev-parse
--git-dir` 回 0），不可達時回 **`unavailable`** 而非 pending。

> ⚠️ 這**沒有**關閉一般性缺口。一條跑起來但參數寫錯、回退出碼 1 的訊號，仍與「檢查為否」
> 同形。`evaluate_signal()` 只升級 127（找不到指令）與 126（不可執行）這兩個語意無歧義的碼；
> git 的 128／129 刻意不升級，因為它們同時用於「不是有效物件」，升級會把「分支還沒出現」
> 誤報成壞掉的訊號。

---

## 隔離：SHALL 顯式安排，不會自動發生

**背景 session 不會被自動隔離，兩個 repo 皆然。** 實測：未帶容器旗標啟動的背景 session，第一
次編輯檔案時拿到的是一則要求先 `EnterWorktree` 的 guard，而不是一個已建好的容器。目標 repo
的隔離設定比指揮站 repo 更積極，**也不改變這件事**——「目標 repo harness 比較弱所以不會自動
開容器」這個推測方向是反的。

指揮站 repo 用起來像自動，是因為它自己的工作流指令層會**顯式建容器**（打開一個既有專案時
執行 `git worktree add`、開一輪內容提交時開臨時 worktree）。**目標 repo 不能假設有這一層。**

隔離是**被宣告過的 session 狀態**，不是 cwd 的性質：同一個 session 在 `EnterWorktree` 前後，
環境判定由 `in_worktree=False` 翻為 `True`，編輯 guard 隨之從拒絕翻成允許。

所以兩條路擇一，**不能兩條都不走**：

| 路 | 怎麼做 | 什麼時候選 |
|---|---|---|
| 指揮站建容器 | 啟動帶 `--worktree <name>`，`dispatch_plan(…, container_name=…)` | 節點要開新分支 |
| worker 自己進容器 | 派工單**第一個動作**寫 `EnterWorktree`（或該 repo 工作流的等價步驟） | 節點續做既有分支，或走目標 repo 自己的工作流（該工作流會自建容器） |

沒安排隔離的 worker 落在目標 repo 的預設分支上未隔離，接著只有兩條路：以無 guard 的 shell
路徑寫進 shared checkout，或卡在 guard 上空轉。兩者都不是可接受的預設。

---

## 容器名推不準 ⇒ 訊號只能走 commit

實測：指揮站把 session 命名 `xrepo-smoke-probe`，worker 自行隔離後**容器叫 `xrepo-smoke`**、
分支 `worktree-xrepo-smoke`。走目標 repo 自己工作流的 worker 更是如此——容器由那個工作流命名。

所以跨 repo 節點：

- `done_signal` **SHALL 是 commit 形態**：`git log --oneline <你指定的分支> | grep -q <marker>`。
- 「worker 容器內的檔案」那條落點**不可用**。`worker_output_dir(node)` 對跨 repo 節點**直接
  拒答**（未給 `container_name` 就 raise），而不是回一條猜的路徑——回猜的路徑，失效形態是
  永遠 pending；在呼叫點就炸，至少看得見。
- **分支名由你在邊界段指定**，worker 不得自行改用他名。分支名平常是 worker 的自治範圍
  （§3 元件 4），但那條管的是「指涉他人」；這裡是「你對受派者下達的命名」，兩者不衝突。

---

## 派工單：留在指揮站，靠 `--add-dir` 讓 worker 讀到

**SHALL NOT 把派工單寫進目標 repo。** 兩個理由：

1. 指揮站落檔派工單的目錄是指揮站 repo 自己的目錄約定。寫進一個別人的 repo，是把自己的
   形狀外溢到一個不認得它的版本庫，而那個 repo 的 `.gitignore` 沒理由忽略它。
2. plan 的單一寫入者不變式建立在「只有一份 plan」上。依 worker 所在 repo 分散存放，同一次
   dispatch 會有 N 份作戰圖，而「哪一份是真的」沒有答案。

`brief_path()` 收 `repo` 參數，理論上寫得進去——**那個參數是測試注入 `tmp_path` 的通道，不是
跨 repo 的入口。**

worker 靠 `--add-dir <指揮站主 checkout>` 讀派工單（實測 0 失敗、任意深度）。這條授權同時
**擴大了 worker 對指揮站 repo 的可及範圍**，所以它 SHALL 出現在你的單一確認畫面上，跟
`base_ref` 一樣是被看過的決定，不是隱式副作用。

---

## `repo` 參數是過載的

這是最容易踩、且踩了看起來完全正常的坑：

| 函式 | 它的 `repo` 指誰 |
|---|---|
| `mission_path()` / `brief_path()` / `write_brief()` / `write_mission()` | **指揮站 repo** |
| `worker_output_dir()`（舊簽名） | **目標 repo** |

把同一個值傳下去，會把整份作戰圖搬進目標 repo。所以 `worker_output_dir()` 改成收 `node`
（自 `workspace_repo` 推目標 repo），`dispatch_plan()` 的 `repo` 明確只指指揮站。

---

## 用 `dispatch_plan()` 取事實，別手推

    from session_dispatch.mission_plan import dispatch_plan
    dp = dispatch_plan(node, "<mission-id>")
    # dp.cwd                   launch 指令從哪裡下
    # dp.add_dirs              要顯式授權的附加目錄（跨 repo 才非空）
    # dp.brief_abs             派工單絕對路徑（恆在指揮站）
    # dp.container_name        你要建的容器名；worker 自建時為空
    # dp.container_from_worker True ⇒ 派工單第一個動作 SHALL 是 EnterWorktree

它**刻意不回傳 CLI 字串**。旗標形狀是 CLI 的非正式契約、會隨版本漂移，其 SSOT 是 SKILL.md §4；
複製進這層會讓同一件事有兩個真相層，而兩者會各自演化。它回答的是 repo 推導得出的**事實**，
渲染成指令是你的事。

它是 plan-only 的：純讀，不開 session、不建容器、不寫檔。

---

## worker 那頭沒有指揮站 repo 的原語

跨 repo worker 的 workspace 是別的 repo，**指揮站 repo 的 Python 模組不在它的 import path
上**。派工單裡任何「跑指揮站 repo 內部模組」的指示對它都是壞指令。要它跑檢查，只能給不依賴
本 repo 的 shell 命令，或給該 repo 自己的工具。

---

## 收尾：容器歸你，分支不歸你

**`plan_worker_teardown(node)` 逐節點回答「這個 worker 的容器還在不在、能不能拆」。**

    from session_dispatch.mission_plan import plan_worker_teardown
    tp = plan_worker_teardown(node)
    # tp.state             disposed / pending / unavailable
    # tp.worktree_path     容器在哪；定位不到為 None
    # tp.dirty_files       容器內未 commit 的變更（訊號 fire ≠ 產物落地）
    # tp.blocking_sessions 還坐在裡面的 live session
    # tp.unpushed          分支上有從未 push 的 commit（`claude rm` 的第二種拒絕條件）
    # tp.branch_merged     分支是否已併入 base——**事實，不是你的待辦**
    # tp.steps             收尾指令，已排好順序
    # tp.notes             要被看見但不改變 state 的觀察

它同樣是 plan-only：純讀，不 unlock、不 remove、不刪 session。

### 定位靠**你指定的分支**，不靠容器名

容器名推不準（見上一節），但收尾要的是**容器路徑**——而那可以用 `node.work_branch` 在目標
repo 內反查。所以跨 repo 節點 SHALL 宣告 `work_branch`：

    MissionNode(
        …,
        work_branch="feat/x",   # ← 你在邊界段指定給 worker 的那個分支
        done_signal="git log --oneline feat/x | grep -q '[N1-DONE]'",
    )

**別想從 `done_signal` 反解分支名。** 那是自由格式 shell，上面只是最常見的一種寫法；用正則
從中挖分支名會在第一個改寫法的節點上失敗，而失敗形態是「定位到 `None`」——與「容器真的不在
了」外觀相同。

主路徑選分支而非 session roster，是因為**收尾的典型時機正是 session 已經停掉之後**，roster 那
時已經查不到。roster 降為輔路徑：補 `blocking_sessions`，以及在分支反查未命中時告訴你「那個
session 其實坐在哪」。

### 三態的判準是「有沒有可信錨點」，不是「有沒有找到」

| state | 意思 |
|---|---|
| `disposed` | **正面斷言**：以可信錨點枚舉過目標 repo 的 worktree，無一屬於本節點 |
| `pending` | 容器還在，待處置 |
| `unavailable` | 目標 repo 不可達，或**未宣告 `work_branch`**（無錨點） |

沒宣告 `work_branch` 時枚舉一樣全部落空，外觀跟已處置一模一樣。此時回 `disposed` 就是**用
「我沒找到」冒充「它不存在」**——同一個病灶在這個工作流已經出現過三次（錨錯 repo 的訊號、
`worker_output_dir()` 回猜的路徑、這裡），所以它在呼叫點就拒答。

### `steps` 不含分支刪除，這是刻意的

`worktree unlock` → `worktree remove` → `claude rm`，每項顯式帶 `git -C <目標 repo>`。

- **`unlock` 不可省**：`claude stop` 之後 lock 仍在，少這步第一個指令就失敗。
- **沒有 `git branch -D`**：分支去留是目標 repo 的政策。依指揮站的分支慣例去判定一條外來
  分支，一律落入未知類別，據此產生的建議不會報錯、只會錯。同理，**指揮站 repo 自己的收尾
  流程也不可被指向目標 repo**——那類流程的步驟裡常內含推送到主線的動作，指過去不會報錯，
  只會錯在別人的版本庫上。
- **`claude rm` 處置的是 session 不是容器**，所以容器處置的判定不以它成敗為準。否則「session
  已刪但容器還在」會變成一個表達不出來的狀態，而那正是最該被看見的那個。

### worker 沒被隔離時

定位到的路徑若就是目標 repo 的主 checkout，代表**隔離從未被安排**（見上面「隔離」一節）。
此時 `steps` 不會提供移除該路徑的指令——那不是可拆的容器——但這件事會進 `notes`。產物落在
哪、有沒有污染對方的 shared checkout，要另外確認。

### 收尾義務要出聲，不是記得就好

合成報告 SHALL 逐節點列處置狀態，`pending` 與 `unavailable` 分開標示。**完成訊號全綠不代表
容器已回收**——這兩件事在本工作流被刻意分開，因為它們曾經一起沉默。
