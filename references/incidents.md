# session-dispatch 事故簿

這份文件是 session-dispatch 規範條文的事故簿：收錄每條規則各自的實測來源，讀 SKILL.md
時想知道某條規則「為什麼這樣定」就來這裡查。

規範條文住 `SKILL.md`；本檔存**它們各自的實測來源**。分開的理由是漸進揭露——SKILL.md 要能
被一次讀完，而事故敘事是「需要時才展開」的那一層。

每條的格式固定：**條文 → 當時發生什麼 → 為什麼那條規則擋得住它**。
沒有實測來源的條文不該進 SKILL.md，也不該出現在這裡。

> ⚠️ **推翻既有條文的條目 SHALL 回填「推翻了哪一節的哪一句」，且該句 SHALL 在同一次修訂
> 中被改寫。** SHALL NOT 只在這裡就地追加校正，而讓被推翻的原句原封不動留在 SKILL.md
> 前面幾節。實據見下方「§2 的『免費強制』」——那條矛盾**不需要任何外部實測就能發現**
> （§4 早已寫明穿透路徑），它之所以存活兩個月，正是因為 §4 那條實測校正是就地追加的，
> 追加者只修了自己那一節。一份把「沉默被讀成通過」當核心原則的文件，內部矛盾的存活時間
> 不該取決於下一個讀者恰好同時讀了哪兩節——能靠結構強制就別靠自律。

---

## 元件 1：附驗證指令 ≠ 執行過驗證指令（2026-08-19）

**條文**：已查證的前提 SHALL 附驗證指令**並貼上實跑輸出**。

一份派工單寫了兩條「已查證的前提」，各自括號附了成本近零的驗證式——**兩條都是從舊記錄
抄來的，指令從未被執行**。兩條都被 worker 當場證偽：

| 派工單的斷言 | 附的驗證式 | worker 實跑 |
|---|---|---|
| 「全 repo 無任何 fetch 路徑帶某個旗標」 | `grep` 某個檔案內是否命中該旗標（宣稱回 0） | **回 3**——前一天已由另一個變更修掉 |
| 「某份文件的路由矩陣缺一列」 | 讀該文件 | 該列**前一天已補齊**，段落標題逐字寫著補齊日期 |

兩條都是「立案時未查證現況」的經典形態，而它們發生在一份**第一元件就叫「已查證的前提」**
的派工單裡。

**為什麼貼輸出擋得住**：檢查表能驗「有沒有附指令」，驗不了「跑沒跑」——而兩者在派工單上
外觀完全相同，是「沉默被讀成通過」的又一個例子。輸出是無法從舊記錄抄來的東西：它帶著那一
刻的事實。輸出與當下不符時，下游得到的是一個**訊號**而不是一個錯誤的前提。

---

## 元件 4：分支名是 worker 的自治範圍（2026-08-19）

**條文**：指涉並行節點 SHALL 用「檔案路徑 ＋ 節點 id」，SHALL NOT 用分支名。

指揮站在派工單裡告訴 C 節點「B 節點握有某份檔案的寫入權」，並以 B 的 worktree 預設分支名
指涉。

B 出於**正確理由**開了另一個名字的分支。於是 C 去複驗那份檔案時，看的是那個空的預設
分支——仍停在 base commit、零 commit，**對面的編輯從未落到 C 看得見的 ref**。

C 誠實回報了這個盲區，而它沒被坑到的原因是派工單另外要求它「findings SHALL 以內容
pattern 而非行號錨定」——**保險生效了，但保險不該是唯一防線**。

**worker 改分支名的兩個正當理由**（都會發生，都不需要指揮站同意）：

1. 為通過該 repo 自己合併流程的型別 guard 而改名成慣例分支前綴
2. 目標分支已被別的 worktree 持有，git 不准同一分支在兩處 checkout，只好改切既有容器

這與 §2「`done_signal` 不得綁在 worker 可正當改名的東西上」是**同一條原則的兩個出口**：
一個管完成判定，一個管邊界宣告。檔案路徑不會被 worker 改名，節點 id 由指揮站發放。

---

## `--worktree`：派工既有分支時是純浪費（2026-08-19）

**條文**：派工單指定既有分支時 SHALL 省略 `--worktree`。

同一輪的兩個節點都被要求「在既有的 `feature/<x>` 上續做」，兩個都帶了 `--worktree`。
兩個 worker 都自行改用 `EnterWorktree(path=…)` 切進**既有**容器——因為目標分支已被那些
容器持有，git 不准同一分支在兩處 checkout。

結果：**兩個新建的容器與分支全程零使用**，收尾時還多刪兩個空分支。

**代價不只是浪費**：`git worktree list` 與 roster 會出現與實際工作位置不符的條目，
而指揮站正是靠它們判斷「誰在哪裡、誰持有什麼」——這與上一條的邊界問題同源。

---

## `claude rm` 的第二種拒絕條件（2026-08-18）

**條文**：收尾 SHALL 對每個 worker worktree 做出明確處置。

`SKILL.md` §7 記的是「目標 worktree 有未 commit 變更時拒絕」。實測撞到的是另一句：
`kept <id> — worktree has commits that are not pushed anywhere`——兩個 worker worktree
**working tree 皆乾淨**卻仍被拒。

成因：feature 分支上有從未 push 的 commit，而該 repo 自己合併流程的 squash 步驟**保證**
那些 commit 永遠不會被 push（分支 merge 後即刪）。於是每次走完那個合併流程再收 worker
session，這個拒絕都會出現。

**「未 push」與「未落地」在此脫鉤**：commit 的**內容**早已在 main，只是**那些 SHA** 沒被
push 過。`claude rm` 問的是後者、人關心的是前者。

正確順序（實測可行）：`git worktree unlock` → `git worktree remove` → `git branch -D`
→ `claude rm <id>`。**或**在收尾前先 push 分支——那會讓這條拒絕條件消失。

### 補記（跨 repo dispatch 某次實跑收尾時）：`unlock` 不是可省的第零步

指揮站收 `xrepo-verify` 時逐字撞到：

```
$ claude rm e767de51
kept e767de51 — worktree has commits that are not pushed anywhere
$ git worktree remove .claude/worktrees/xrepo-verify
fatal: cannot remove a locked working tree, lock reason: claude session xrepo-verify (pid 30327 …)
```

**`claude stop` 之後 lock 仍在**——session 已停、roster 已查不到它，但 `git worktree` 那層的
鎖沒有隨之釋放。於是「先 `claude stop` 再照 §7 的繞法走」這條看似最自然的順序，**第一個
指令就過不去**。`git worktree unlock <path>` 之後三步全部順利。

> ⚠️ **本條回填**：`SKILL.md` §7 原文寫「或走 `worktree remove` → `branch -D` → `claude rm`」，
> **缺 `unlock`**。已於同一次修訂改寫（現為四步且明說 `unlock` 不可省）。
>
> 這正是本檔檔頭那條規約要防的形態，而且**它在本檔內部就已經發生過一次**：`unlock` 早在
> 上面那段（2026-08-18）就寫進了事故簿，SKILL.md 的規範摘要卻沒跟上，於是**隔天**照著
> SKILL.md 走的人（就是我）又撞了一次。**事故簿寫對了不算數——被讀的是 SKILL.md。**
> 落差存活了一天就發作，正說明這條規約該是結構強制而非自律。

---

## §2 的「免費強制」：一條比實際強的安全保證（2026-08-19）

**推翻了**：SKILL.md §2 原句「這個限制其實幫你**免費強制了「plan 只由指揮站寫」**，所以不要
試圖繞過」，以及 `worker_output_dir()` docstring 的「這個限制其實是**免費的不變式強制**」。
兩處已於同一次修訂改寫。

**條文**：文件描述此限制時 SHALL 說它是**慣例**、強度取決於 worker 選了哪個工具，
SHALL NOT 描述為免費強制，亦 SHALL NOT 描述為「多層防護疊加」。

### 實跑矩陣

目標一律為主 checkout 內一個被 `.gitignore` 排除的目錄（`git check-ignore` 命中，探測期間
對指揮站的 `git status` 零足跡），**mission 目錄全程只做唯讀探測、未寫入**。

| # | 動作 | 結果 |
|---|---|---|
| 1 | shell 小額寫入 shared checkout | ✅ 穿透 |
| 2 | shell **500 行**寫入 shared checkout | ✅ 穿透 |
| 3 | shell 複合寫入（`printf > path && cat path`） | ✅ 穿透 |
| 4 | 檔案編輯工具 → 所在 repo 的 shared checkout | ❌ 被拒（「Edit the worktree copy…」） |
| 5 | 檔案編輯工具 → `/tmp` | ✅ 放行 |
| 6 | Python `Path.write_text` → shared checkout | ✅ 穿透 |
| 7 | `test -w` / `os.access(W_OK)` on mission 目錄 | 皆回可寫 |
| 8 | `git -C <自己容器之外>` | ❌ 被拒（git 導向） |
| 9 | 含 `cd … && git init` 的複合命令 | ❌ 被拒（無法靜態證明留在容器內） |

**寫入量不是判準。** 被攔的兩次（#8 #9）都與寫入無關——一次是 `git -C` 導向，一次是命令的
可靜態解析性。第 5 列另外校正了 guard 的範圍：檔案編輯工具擋的**不是**「容器之外」，是
**所在 repo 的 shared checkout**；`/tmp` 與工作暫存目錄都放行。

### 為什麼這條擋得住

原句的危害不是不精確，是**它會讓讀者決定不必再加防護**。準確版本反過來指出還缺什麼：

> 檔案編輯工具會拒絕越界寫入，shell 與 Python 不會。因此「plan 只由指揮站寫」是一條
> **慣例**，其強度取決於 worker 選了哪個工具——**而偏好 shell 的執行模式正把它推向那一
> 側**。要讓它成為不變式，需要工具層以外的機制。

順帶校正一條轉述：早期版本寫「小額 shell 寫入繞得過、大量才被攔」。**不可重現**——那多半是
把「我下了一條複雜命令被擋」誤歸因成「我寫太多被擋」。

### 兩條失效方向相反，只描述一條會誤導

- worker 選檔案編輯工具 → 被擋、改寫到容器內同名路徑 → 回報完成而**訊號永不 fire**。
- worker 選 shell → **靜默寫穿** → 訊號照常 fire，而**不變式已破**。

docstring 原本只寫了第一條，於是讀者會以為第二條不存在。

---

## 跨 repo：錨錯 repo 的訊號被判成 pending（2026-08-19）

**條文**：節點 SHALL 宣告 `workspace_repo`；求值 SHALL 錨定該 repo；不可達時回 `unavailable`。

`done_signal` 是自由格式 shell，寫 `git -C /abs/path log …` 今天就穿得過去——**跨 repo 節點
本來就表示得出來**。所以這條的理由不是表達力，是失效模式：

    rel（錨錯 repo）: SignalOutcome(state='pending', detail="'git <command> [<revision>...]…'")
    abs（正確錨定）: SignalOutcome(state='done')

錨錯 repo 的 git 指令**實際失敗了**（吐 usage 錯誤、退出碼非零），卻被判成 `pending`——因為
`evaluate_signal()` 的 `unavailable` 只在 `TimeoutExpired` / `OSError` 觸發。

**為什麼這條擋得住**：宣告 `workspace_repo` 後，求值前先驗該 repo 可達，不可達回
`unavailable`。這把「錨點壞了」從進度狀態的一種，還原成能力故障——正是三態設計要消除的
失效。

**射程**：只擋得住錨點錯誤與退出碼 126／127（跑不起來）。一條跑起來但參數寫錯、回退出碼 1
的訊號，仍與「檢查為否」同形。**別把這個修補描述成「三態求值已能辨識壞掉的訊號」**——那會
是同一個過強宣稱的第二個版本。

---

## 跨 repo：容器名不在指揮站控制內（2026-08-19）

**條文**：跨 repo 節點的 `done_signal` SHALL 為 commit 形態；`worker_output_dir()` 對跨 repo
節點拒答。

指揮站把 session 命名 `xrepo-smoke-probe`，worker 自行隔離後**容器叫 `xrepo-smoke`**、分支
`worktree-xrepo-smoke`。走目標 repo 自己工作流的 worker 更是如此——容器由那個工作流命名。

**為什麼拒答比回猜的路徑好**：回一條猜的路徑，失效形態是 `test -f` 永遠回非零 → 永遠
`pending` → 指揮站永遠等；在呼叫點 raise，至少看得見。這與上一條是同一個判斷：**沉默的錯
比大聲的錯貴得多。**

同時修掉一個過載：`repo` 參數在 `mission_plan` 裡對 `worker_output_dir()` 指**目標 repo**，
對 `mission_path()` / `brief_path()` 指**指揮站 repo**。把同一個值傳下去會把整份作戰圖搬進
目標 repo，而呼叫點看起來完全正確。

---

## 跨 repo：背景 session 不會自動隔離（2026-08-19）

**條文**：worker 的隔離 SHALL 顯式安排（啟動帶容器旗標，或派工單第一個動作 `EnterWorktree`）。

未帶容器旗標啟動的背景 session，第一次編輯檔案時拿到的是一則要求先 `EnterWorktree` 的
guard，**而不是一個已建好的容器**。指揮站自身與煙霧測試的跨 repo worker 是同一形態。

目標 repo 的隔離設定比指揮站 repo **更積極**，也不改變這件事——「目標 repo harness
比較弱所以不會自動開容器」這個推測方向是反的。

隔離是**被宣告過的 session 狀態**，不是 cwd 的性質：同一個 session 在 `EnterWorktree` 前後，
環境判定由 `in_worktree=False` 翻為 `True`，編輯 guard 隨之從拒絕翻成允許。

**為什麼這條擋得住**：指揮站 repo 用起來像自動，是因為它自己的工作流指令層會顯式建容器。
**目標 repo 沒有這一層**，而沒安排隔離的 worker 落在對方 main 上，接著只有兩條路：以無
guard 的 shell 路徑寫進 shared checkout，或卡在 guard 上空轉。
