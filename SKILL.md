---
name: session-dispatch
description: >
  以當前 session 作為「指揮站」，把一輪討論結束後的多個下游任務派給獨立 background
  session 或 subagent，並收斂回來。涵蓋 mission 規劃、節點執行形態（哪些規約綁
  session、哪些跨形態通用）、一行開具名 session、派工單五元件強制檢查表、star 拓樸
  協調規約（訊息只當 doorbell）、完成訊號為真相層、跨 repo 派工（worker 在別的 repo）、
  worker 容器收尾判定、以及收斂義務。
  Use when: 討論結束後有 2 個以上可分開執行的下游任務、需要並行推進多個 worktree、
  要把長跑任務交給獨立 session 並在完成時被通知、要把 worker 派到另一個 repo、
  或要對一批 subagent 發正式派工單。
---

# session-dispatch

把「一輪討論結束 → 發出 N 個下游任務 → 收回來」變成可重複執行的工作流。

**底層原語**：`session_dispatch/mission_plan.py`（plan 讀寫、三態訊號求值、ready 計算、
派工單 lint、`dispatch_plan()` dispatch 事實推導、`plan_worker_teardown()` 容器收尾判定）、
`session_dispatch/live_sessions.py`（roster 枚舉、`name_is_taken()` 撞名檢查、
`sessions_holding()` 容器持有者、`coverage_note()` 覆蓋範圍轉述）。
**規約 SSOT**：`SPEC.md`。

---

## 0. 先決定要不要用

派工不是免費的。先過這四軸——**任一軸命中右欄才考慮開 session**：

| 軸 | → 用 subagent | → 開獨立 session |
|---|---|---|
| **Human-in-loop** | 只要結果，過程不需要人看 | 過程需要人看著、中途可能要轉向、要能隨時接管對話 |
| **資訊檢視** | 摘要即足夠 | 需要完整 transcript、狀態面板、逐步觀察 |
| **Model 獨立性** | 用預設 model 即可 | 需要與主執行緒不同的 model／effort，且兩邊 cache 互不干擾 |
| **生命週期** | 綁 caller，做完就結束 | 工作跨越一個對話的壽命（長跑、跨日、可 detach 再回來） |

**反向判準**：結果之外的一切都是主對話的噪音（大量搜尋結果、log、讀檔），且任務已被拆成
有界範圍——那正是 subagent 存在的理由，**不因為 session 可用就升級**。

開 session 有三項隱性成本，決策時要算進去：

1. **冷啟動不便宜** — 新 session 要重跑你的 session start 協定（讀 `CLAUDE.md` / `rules/`
   之類的常駐脈絡）。subagent 拿 caller 給的 prompt 就跑，沒有這筆開銷。
2. **Quota 倍速** — N 個並行 session ＝ N 倍消耗。
3. **收斂責任回到人** — subagent 的結果自動回 caller；session 的產出散在各自 transcript
   與 worktree 裡，收斂靠訊息自律或人工。

### 專屬前置問題一：分開做真的比較快嗎

每個 worker 都要重跑一次冷啟動。三個節點就是三份。**串著自己做完常常比較快**，尤其當
節點之間有順序依賴時——那種情況下並行度是 1，你付了三份冷啟動卻沒買到任何並行。

派工划算的典型形狀：節點之間**真的可以同時跑**，且每個節點的工作量遠大於冷啟動成本。

### 但速度不是唯一的划算理由

第一次實跑的並行度就是 1——兩個節點有順序依賴，付了兩份冷啟動、買到零並行，純以速度論
自己順著做完更快。

但兩個 worker 交出的內容不是我會自己想到的：一個抓到我的 grep 判準射程外的殘留，一個
質疑了我對自己模組的能力歸因、直接改善了 API 設計。

所以判準有兩條，各自獨立成立：

1. **這幾件事分開做會不會比較快？**（並行度 > 1 才成立）
2. **這件事我自己做會不會有系統性盲區？**（獨立視角，與並行度無關）

第 1 條不成立時第 2 條仍可能成立。**審查、驗證、找自己的錯**這類任務屬於第 2 條——它們的
價值來自「不是同一顆腦袋」，不是來自同時進行。

### 專屬前置問題二：指揮站要醒著多久

star 拓樸要求指揮站在 worker 回報時還在。指揮站 idle 超過 prompt cache 的存活時間後被
訊息喚醒，要付全額 cache 重建成本（單價是 cache read 的十倍量級）。

所以：**節點的預期時長若遠超 cache 存活時間，改用「你自己稍後回來查」而非「掛著等」**。
mission plan 是落檔的，指揮站關掉再開一個新 session 讀 plan 也能收斂。

---

## 1. 規劃 mission

順序固定，因為每一步都是下一步的輸入：

1. **任務分解** — 拆成可獨立驗證的節點。無法獨立驗證的不是節點，是同一個節點的兩半。
2. **依賴關係** — 誰要等誰。`depends_on` 只記直接依賴。
3. **執行形態** — 每個節點是 `session` 還是 `subagent`（見下）。
4. **邊界分配** — 每個節點動哪些檔案、哪條分支；哪些節點需要獨佔主 checkout。
5. **完成訊號** — 每個 `session` 節點宣告一條 `done_signal`（見 §2）。
6. **base ref** — 每個 `session` 節點從哪裡開工（見 §4 的隱式繼承陷阱）。
7. **命名** — `<mission-id>-<node-id>`，並驗證不撞名（見 §4）。

`mission_id` 用 `<YYYY-MM-DD>-<slug>`，跨日殘留時一眼可辨。

plan 檔形狀見 `examples/mission_plan.example.json`；欄位語意見
`session_dispatch/mission_plan.py` 的 dataclass docstring。

### 節點形態：哪些規約綁 session，哪些跨形態通用

節點不一定是獨立 session。實跑規模最大的一次（13 個 agent）節點多數是 **subagent**——由
指揮站 spawn、共用檔案系統、結果直接回 caller。要不要用 subagent 的判準見 §0，這裡只講
**形態決定哪些條文適用**。

| 規約 | `session` | `subagent` | 為什麼 |
|---|---|---|---|
| 派工單五元件（§3） | ✅ | ✅ | 跨形態通用。那次三個「可推翻的預設值」都是被 subagent 查證後否定的 |
| 產物落點要**驗證**不採信 | ✅ | ✅ | 「說寫好了」與「寫在你以為的地方」是兩件事，與形態無關 |
| 收斂合成報告（§7） | ✅ | ✅ | 派工單開的支票不因對象而免付 |
| `done_signal`（§2） | ✅ | ❌ | 它是為了「隔著通道判定他方狀態」而存在；subagent 的結果直接回到你的 context |
| `base_ref`（§4） | ✅ | ❌ | subagent 沒有自己的 worktree 與分支 |
| 撞名檢查（§4） | ✅ | ❌ | 沒有名稱可定址 |
| star 拓樸（§5） | ✅ | ❌ | 沒有 peer 通道 |
| worktree 處置（§7） | ✅ | ❌ | 沒有 worktree |

`MissionNode.shape` 預設 `session`——**預設落在約束較嚴格的一側**，漏填得到的是完整約束
而非豁免。subagent 節點的產物落指揮站工作目錄下的 mission 子目錄；若指揮站自己也在一個
會被清掉的 worktree 裡，**收尾前要把要留的搬走**——與 §2 的 worker 產物同一條紀律。

---

## 2. 完成訊號：整個工作流的真相層

**每個 `session` 節點 SHALL 宣告一條 `done_signal`：一條 shell 指令，退出碼 0 即完成。**

三種形態覆蓋目前所有場景：

| 形態 | 例 |
|---|---|
| branch 上出現 commit | `git log --oneline <branch> \| grep -q "<marker>"` |
| 檔案存在／內容命中 | `test -f <path>`、`grep -q "<pattern>" <path>` |
| 任意檢查指令 | `python -m <module> --check` |

### ⚠️ 產物落點：別叫 worker 寫 mission 目錄

要求 worker 寫 mission 目錄的絕對路徑，**若它用檔案編輯工具**，會被擋下並改寫到自己容器內
的同名相對路徑——然後回報「做完了」，而你的 `done_signal` 永遠不 fire。

**但這不是「免費強制了『plan 只由指揮站寫』」，也不是多層防護疊加。** 實測：檔案編輯工具
拒絕越界寫入，**shell 與 Python 不會**——500 行 shell 寫入照樣穿透，權限層也回可寫；被攔的
兩次是 `git -C` 導向與命令無法靜態解析，都與寫入無關（**寫入量不是判準**）。所以那是一條
**慣例**，強度取決於 worker 選了哪個工具——**而偏好 shell 的執行模式正把它推向那一側**。
選 shell 的 worker 靜默寫穿：訊號照常 fire，不變式已破。完整矩陣見 `references/incidents.md`。

改讓訊號指向 worker 寫得到的地方：

| 落點 | `done_signal` | 適用 |
|---|---|---|
| worker 容器內的檔案（`worker_output_dir(node)`） | `test -f <容器>/<path>` | 一次性回報，且**容器是你建的、你命名的**。收尾清理前要先讀走 |
| worker 分支上的 commit | `git log --oneline <branch> \| grep -q <marker>` | 產物需活過清理；**跨 repo 節點只能走這條**（見 §4） |

### 三條硬規

1. **worker 的訊息不是完成判定。** 訊息可能被 hold 住等人批、被設定拒收、卡在待讀上限、
   或 worker 已被清掉。訊號則是 worker 產出的東西本身，它不會因為通道故障而消失。
2. **求值失敗 ≠ 未完成。** `evaluate_signal()` 回三態；`unavailable`（指令跑不起來）
   SHALL 單獨列出，SHALL NOT 併入 pending——把壞掉的訊號渲染成「還在跑」，指揮站會永遠
   等一個不會到來的完成。
3. **宣告不出 `done_signal` 的節點不得派工。** 無法用一行指令說清楚「怎樣算做完」，代表
   任務定義還沒收斂。這時該做的是繼續討論，不是派工。第 3 條同時是派工單品質的前置閘門
   ——它逼你把驗收條件想清楚。

---

## 3. 派工單：五元件與檢查表

**派工單 SHALL 落檔**（`brief_path`），開 session 的 prompt 只帶絕對路徑。理由：避開把
數百字塞進 shell argv 的引號地獄、讓派工單成為可審閱可重跑的 artifact、以及讓「訊息只帶
指針」在 dispatch 這一段也成立。

### 核心原則

**派工單的字數只該花在下游查不到的東西上。**

agent 的 context 有兩個跟人相反的性質：記憶為零（它不知道你剛才半小時在想什麼），檢索極
便宜（它三秒能 grep 完整個 repo）。所以**給座標，不要給知識**——凡是它自己 grep 得到的，
給檔案路徑就好；凡是只存在於這輪對話裡的，一個字都不能省。

對人類同事恰好相反。同事有累積的記憶，但檢索很貴——你叫他去翻某份規格，他要花二十分鐘
而且可能翻不到，所以對人交代事情要給結論。對 agent 給結論反而是浪費，因為結論是它自己
能便宜生成的東西，而你替它生成的結論還帶著「可能是錯的」風險。

寫壞的派工單多半往反方向壞：花大篇幅解釋背景（查得到），然後用「有問題再問我」打發掉
協調資訊（查不到）。

### 五元件檢查表（缺一不得送出）

| # | 元件 | 可觀察的證據 |
|---|---|---|
| 1 | **已查證的前提附驗證動作＋實跑輸出** | 每條要下游採信的斷言，附一條成本近零的複查指令，**且附上你剛才實跑它得到的輸出** |
| 2 | **線索與指令分開標示** | 開放性線索段落顯式標明「不是指令」 |
| 3 | **判斷寫成可推翻的預設值** | 傾向 + 理由 + 顯式授權推翻 |
| 4 | **邊界描述誰握著什麼** | 列並行 session、分支佔用、不可動的檔案 |
| 5 | **每個回報項附用途** | 說明該回報拿去做什麼 |

**檢查針對證據，不針對標題。** 有一個叫「邊界」的段落但內容是「小心一點」，不算通過。

### 機械下限

    python3 -c "from session_dispatch import lint_brief, brief_path; \
    print(lint_brief(brief_path('<mission>','<node>').read_text()))"

`lint_brief()` 抓**結構性遺漏**，兩種形態各有訊息：**整段缺席**（五個元件任一段沒寫）與
**段落存在但內容缺項**（有前提段卻沒有驗證指令、線索段沒標「不是指令」、回報項沒寫用途）。
兩者訊息刻意不同，因為下一步動作不同——前者新增段落，後者補內容。

段落標記寫 `## 邊界` 或 `—— 邊界 ——` 都認：**lint 對格式寬容、對內容嚴格。** 假陽性會讓人
開始忽略 lint，那比沒有 lint 更糟。

它抓**不到內容空洞**——「（我要拿去參考）」會通過。**所以 lint 綠燈不等於派工單合格**：
聚合綠燈只驗證「有沒有」，不驗證「對不對」。

> 早期版本把元件 1／2 寫成「段落存在時才檢查」，於是**整段忘了寫反而一聲不吭**——恰好放過
> 最該被擋的形態，與它自己宣稱的保證相反。沉默不能既是「沒發生」又是「發生了但沒人說」。

### 為什麼是這五條

- **1**：不附驗證路徑的斷言，下游只能整段盲信或整段重查，兩條都貴。「我確認過這個模組的
  併發模型安全」是壞的；「`grep -c "^## Unread" = 0`」是好的——後者一秒驗完。

  🔴 **只附指令不夠，SHALL 一併貼出實跑輸出**——**附上驗證指令 ≠ 執行過驗證指令**，而兩者
  在派工單上外觀完全相同。輸出讓斷言帶上時間戳：**它與當下不符時，下游得到的是一個訊號而
  不是一個錯誤的前提**。實測見 `references/incidents.md`。
- **2**：agent 對疑問句和祈使句的區辨沒有你想的穩。「或許可以考慮改成 fail-fast？」有很大
  機率被當成指令執行。**顯式標示比措辭修飾有效得多。**
- **3**：只寫「你自己判斷」，下游要從零建立判斷條件；只寫「就照第三種做」，explore 這步就
  只是補文件。帶理由的預設值兩者都不是——**它給了下游一個可以反駁的靶**，而實跑中它確實
  被反駁了三次。
- **4**：worktree 已把檔案系統隔開，衝突實際發生在**分支語意層**——誰在改主線、誰的分支還
  沒收尾。邊界寫在這層才擋得住事。而且它是純粹的協調資訊，下游 grep 不到。

  🔴 **指涉並行節點時 SHALL 用「檔案路徑 ＋ 節點 id」，SHALL NOT 用分支名。**
  分支名是 worker 的自治範圍（過型別 guard 要 rename、撞上分支已被他處 checkout 要改切既有
  容器）——實測 C 依分支名複驗，看到的是 B 早已棄用的空分支。這與 §2「`done_signal` 不得綁
  在 worker 可正當改名的東西上」是同一原則的兩個出口：一個管完成判定，一個管邊界宣告。
  **檔案路徑不會被 worker 改名，節點 id 由你發放。**
- **5**：知道用途，下游才知道該答到多細。「說明你採用的方案」可能換來一句「改成 fail-fast」；
  「我要拿去判斷會不會影響 X 的 applier 契約」換來的是訊號形狀與 caller 接法。

### 模板

**canonical 副本在 `references/brief.template.md`**——那份是可直接複製的檔案，且由測試釘住
「逐字餵進 `lint_brief()` 回零 findings」。下方是同一份內容的內嵌副本（縮排呈現，不是內容
的一部分），兩者由 `tests/test_mission_plan.py` 比對，改一邊不改另一邊會紅燈。

<!-- BRIEF_TEMPLATE_BEGIN -->

    ## 任務

    一句話說清楚要交付什麼。

    ## 已查證的前提（不用重查）

    每條斷言後面附一條成本近零的驗證指令，**並貼上你剛才實跑它得到的輸出**——
    附指令不等於跑過指令，而兩者在派工單上外觀相同。例如：

    - 該函式找不到目標標題時會 append 到 EOF
      （驗：`grep -c "^## Unread" <path>` → 實跑回 `0`）

    ## 線索（不是指令）

    開放問句與掃描範圍。可附自己的傾向與其依據編號，但不替下游拍板。

    ## 邊界

    - base ref：從 `<base>` 開你自己的分支
    - 不要動：`<branch>`、`<path>`（我在用）
    - 另有 session 在做 `<branch>`，檔案面評估無交集；真撞到代表評估錯了，值得回報

    ## 完成訊號

    `<done_signal 指令>` —— 做到這條指令回 0 就算完成。

    ## 回報（每項附用途）

    1. `<問題>`（我要拿去 `<用途>`）
    2. `<問題>`（我要拿去 `<用途>`）

    ## 停下來的條件

    如果 explore 的結論是這件事不該做，停下來回報，不要硬做完。

<!-- BRIEF_TEMPLATE_END -->

> 最後一段是刻意的。缺了它，下游遇到「這其實不該修」時不知道該停還是該繼續。

> ⚠️ **這份模板曾與 lint 不一致。** 舊版用 `—— 段落 ——` 作標記而 `lint_brief()` 只認 `## `，
> 於是**照著模板寫的派工單會被同一份 skill 指定的檢查判成缺三個段**（實測 3 個 findings）；
> 四份實跑派工單之所以全過，是因為沒有一份照模板寫。現在模板改用 `## `、lint 兩種標記都認、
> 且模板本身進了回歸測試——三件事一起做才關得住這個縫。

---

## 4. Dispatch

### 一行開具名 session

    claude --bg --name "<mission>-<node>" --worktree "<mission>-<node>" \
           --model <model> "讀 <brief 絕對路徑> 並依其執行"

實測（Claude Code v2.1.232）：三個旗標全部被尊重；worktree 落在**主 checkout** 的
`.claude/worktrees/<name>`（即使從別的 worktree 啟動也不巢狀）；git 分支為 `worktree-<name>`
（自動加前綴）。

### ⚠️ 派工單指定既有分支時，SHALL 省略 `--worktree`

`--worktree` 建的是**空的新容器 + 新分支**。派工單要 worker 在既有分支上續做時，那容器
**必然用不到**——目標分支若已被別的 worktree 持有，git 不准同分支兩處 checkout，worker 只能
改用 `EnterWorktree(path=…)` 切進既有容器。

代價不只浪費：`git worktree list` 與 roster 會與實際工作位置不符，而指揮站正是靠它們判斷
「誰在哪裡」——同下一段的邊界問題。實測見 `references/incidents.md`。

判準：**開新分支** → 帶 `--worktree`，派工單寫明第一個動作是 `git switch -c <branch> <base>`；
**續做既有分支** → 省略，派工單改寫明「該分支已被 `<path>` 持有，用 `EnterWorktree(path=…)`
切入」。

### 指揮站自己也在 worktree 裡：三種被擋的動作與繞法

指揮站多半自己就是個 worktree-isolated 的 background session，於是它**也**會撞 guard。實跑
撞到的三種形態與繞法：

| 被擋的動作 | 繞法 |
|---|---|
| `cd` 到主 checkout 後執行 | 不 `cd`。用絕對路徑跑單一命令；`cd` 還會讓 cwd 跨 tool call 持久並重新武裝其他 guard |
| `git -C <sibling worktree>` | 只查自己 worktree 內的等價檔案；需要別條分支的內容時用 `git show <ref>:<path>` |
| 複合命令（`;`、`&&`、`for`、多檔 grep） | 拆成單一命令，或把腳本落檔後以一條純命令執行 |

**但 guard 擋的是 agent 的工具呼叫，不是 Python process。** mission 目錄的路徑從 worktree 內
解析到主 checkout 且寫入成功——這正是 `write_brief()` / `write_mission()` 存在的理由。**同一
條事實也是 §2 那個過強宣稱的反證**：指揮站自己就得繞過 guard 才寫得了 plan。

> ⚠️ 別把這條記反了。一度有結論說「指揮站被隔離所以寫不進主 checkout、只好把 mission 目錄
> 搬到 worktree 裡」——**那是錯的，實測可寫**。真正的落點分歧來自 subagent 形態（見 §1），
> 不是可寫性。**改規格前先實測那條規格假設的前提。**

### ⚠️ base ref 會隱式繼承，必須顯式覆蓋

**worker worktree 的 base 往往是啟動方當下的 HEAD，不是主線**（Claude Code 的
`.claude/settings.json` 若設 `baseRef: "head"` 即如此）。指揮站通常正坐在自己的工作分支上，
於是 worker 會**默默帶著指揮站那些 commits 開工**。

繼承本身不一定錯——當節點的工作正是建立在指揮站尚未合併的變更之上時，那正是想要的。問題在
它是隱式的。因此：

- plan 的每個 `session` 節點 SHALL 有 `base_ref`（漏填在 `MissionNode` 建構期就炸，訊息指名
  欄位與理由；`subagent` 節點沒有 worktree，此條不適用）
- 派工單 SHALL 指示 worker 的第一個動作是 `git switch -c <branch> <base_ref>`
- 確認畫面 SHALL 顯示每個節點的 base

### 跨 repo dispatch（worker 在別的 repo）

**完整 playbook：`references/cross-repo.md`。** 三條不能忘：

1. 節點 SHALL 宣告 `workspace_repo`（**絕對路徑**），否則訊號在你這裡求值——錨錯 repo 的
   git 訊號回 `pending` 而非 `unavailable`，你會永遠等一個不會到來的完成。
2. **隔離 SHALL 顯式安排**：帶 `--worktree`，或派工單第一個動作寫 `EnterWorktree`。背景
   session **不會**自動隔離，兩個 repo 皆然；指揮站 repo 像自動是因為它的工作流層顯式建容器。
3. 派工單留在指揮站，worker 靠 `--add-dir <指揮站派工單目錄>` 讀。cwd／附加目錄／容器歸屬
   一律用 `dispatch_plan(node, mission_id)` 取——**容器名你推不準**，跨 repo 訊號只能走 commit。

### 撞名檢查

**名稱即地址**（跨 session 訊息按 name 定址）。dispatch 前查 roster：

    python3 -c "from session_dispatch import name_is_taken; print(name_is_taken('<name>'))"

撞名時改名，不要靠列表的消歧後綴——那個後綴呼叫方拿不到穩定值。

> 用 `name_is_taken()` 而非手刻 `claude agents --json | ...`，有兩個理由，第二個比第一個更早
> 發作：
>
> 1. roster 的欄位形狀是外部 CLI 的**非正式契約**，手刻的解析散落各處時，格式一漂移就是多處
>    同時壞掉而且各壞各的。
> 2. **在非 TTY 環境下它根本沒有輸出可解析。** `claude agents` 需要互動終端機，background
>    session 裡執行必失敗（`requires an interactive terminal`），於是 `| grep -q <name>` 必然
>    落空——實跑據此誤判「worker 已消失」。**把工具的失敗讀成一個關於世界的斷言**是這裡最容易
>    犯的錯；`name_is_taken()` 的 fail-soft 與 `roster.available` 把這兩件事分開了。

### 單一確認點

開 N 個 session 是燒 quota 且每個都付冷啟動稅的不可逆動作，所以 SHALL 在開跑前**一次呈現
完整計畫並取得單次確認**，SHALL NOT 逐節點多輪問答——確認的價值是讓使用者看一眼，不是讓他
回答問題。

呈現至少含：節點 id、session 名稱、形態、model、base ref、完成訊號、依賴、是否需要獨佔主
checkout。

### 開新 vs 復用

**預設為每個節點開新 session。** 復用既有 idle session 可省冷啟動，但舊 context 與舊分支的
污染難以預測。要復用就顯式指定名稱，並先呈現該 session 的 cwd 與所在分支。

### 需要獨佔主 checkout 的節點

有些節點的工作必須在主 checkout 進行（跨分支 merge、需要全 repo 視角的清理），它們會互相搶
同一個資源。`ready_nodes()` 每輪至多放行一個這類節點——同時派兩個等於製造互等。這類節點的
派工單 SHALL 說明可能撞到誰、以及等待協定。

**閘門以 workspace repo 分組，不是全域。** `needs_exclusive_checkout` 的語意是「需要**其
workspace repo 的**主 checkout」；不同 repo 的主 checkout 是彼此獨立的資源，其鎖不互相排斥。
當成單一資源會把跨 repo 的節點無謂地循序化。

---

## 5. Star 拓樸：誰能跟誰說話

**所有 worker 只跟指揮站說話。worker 之間 SHALL NOT 互傳任務定義。**

允許 worker 完成後直接敲下一棒的門作為 latency 優化，但**訊息內容只准是 mission id + 節點
id**（doorbell），任務定義一律從指揮站產出的派工單取得。

三個理由：

1. **任務定義誕生在指揮站的對話脈絡裡**，而那正是下游 grep 不到的東西。讓 worker 轉述等於用
   它的 context 重新生成派工單——必然失真，且會夾帶轉述者自己的實作細節。
2. **chain 傳定義會讓正確性掛在訊息上**，違反「messaging 是 accelerator 不是 dependency」。
3. **permission laundering**：被 deny 的操作不得請 peer 代做。peer-to-peer 任務傳遞容易滑進
   這個形狀。

### 抑制類訊息必須自帶失效條件

上面三條管的是**資料方向**。還有一類訊息管的是**時序**，而它有自己的失效模式：

> **抑制類訊息** SHALL 宣告自身的失效條件（「若你已進入 X 階段則本訊息作廢」）。接收方
> SHALL 以自身實際狀態為準，SHALL NOT 因收到抑制指令而中斷一個已進入**不可安全暫停階段**
> 的操作。無條件的抑制祈使句 SHALL NOT 被送出。

理由與「worker 說它做完了不算完成」同構：**「你還沒開始吧」也是一個關於接收方狀態的斷言，
而發送方對那個狀態沒有觀測權。** 兩者都該讓真相層壓過訊息。

實例：一個指揮站送出「先別 merge，我這邊要先進」，而接收方當下已經 `git merge --squash`
完成、staged 在共用 checkout 的 index 裡。**那個階段暫停比完成更危險**——它把一個 index race
留在共用資源上。接收方正確的動作是完成並立刻回報，而不是服從。

「不可安全暫停」的判準：**暫停會把一個半完成的狀態留在別人也要用的資源上。** staged 未
commit、持有 lock、寫到一半的檔案都算。

---

## 6. 降級：沒有 messaging 也要能跑完

**正確性只建立在 plan 檔與完成訊號上。** messaging 只縮短 latency。

在 messaging 不可用的環境（舊版 CLI、native Windows、其他 agent harness、功能被關）中，工作
流照跑，只是慢——指揮站改用輪詢完成訊號取代訊息喚醒：

    until <done_signal>; do sleep 30; done; echo NODE_DONE

三條喚醒通道：

| 情境 | 通道 |
|---|---|
| worker 完成並通知 | 跨 session 訊息（快、帶語境） |
| worker 完成但沒通知／訊息沒送到 | 輪詢偵測完成訊號 |
| worker 死亡 | roster 查不到該 session + 訊號未出現 → 人工判斷 |

### 不設等待逾時

**SHALL NOT 設節點等待逾時，SHALL NOT 逾時後升級為人工確認。** 逾時的兩個出口（放棄、問人）
都把並發成本轉嫁回使用者，而那正是本工作流要消除的。改為呈現：該節點已等待多久、該 session
在 roster 中的 `status` / `state` / `pid`，讓人一眼判斷該等還是該 kill。

---

## 7. 收斂：派工單開的是支票

mission 的所有節點終止後，指揮站 SHALL 產出**合成報告**落檔。內容至少含：各節點產出、被否決
的替代方案、跨節點的結論、**以及逐節點的容器處置狀態**。

列出五項回報要求卻只掃一眼結論，下次下游就沒有理由認真回報。

> 🔴 **處置狀態 SHALL 逐節點列出，`pending` 與 `unavailable` 分開標示。**
> 在此之前，漏做收尾**沒有任何訊號**——session 停掉、`done_signal` fire、報告寫完，而一個
> 佔著分支的容器留在別人的 repo 裡，整條鏈全綠。工具路徑只解決「做得到」，報告出聲才解決
> 「不會忘」——無強制機制的義務等同於沒有義務。
>
> 出聲點就是報告，**不做跨日稽核**——那需要持久化 mission 狀態，正是 §8 明令排除的方向。
> 要更強的保證請先正面推翻那條，別漸進繞過。

**跨節點的結論是這一步最容易漏掉、也最值錢的部分。** 實跑中出現過八個互不知情的 agent 各自
指向同一批高風險項——那個訊號只在合成階段看得到，任何單一節點的報告裡都沒有。

### worktree 處置

**worker 的 worktree 不會自動回收。** 清理指令有**兩種**拒絕條件：未 commit 變更，以及
**分支上有從未 push 的 commit**（後者在 squash 流程下結構上必然發生——內容早在主線，只是那些
SHA 沒被 push 過）。解法是先 push，或走 `worktree unlock` → `worktree remove` → `branch -D`
→ 刪 session（**`unlock` 不可省**：session 停掉後 lock 仍在，少這步第一個指令就失敗。實測見
`references/incidents.md`）。

所以收尾 SHALL 對每個 worker worktree 做出明確處置：提交／捨棄／保留待查。**完成訊號與產物
落地是兩件事**——訊號說工作做完了，未 commit 變更說產物還沒落地。

**worker SHALL NOT 自行收尾自己的容器**——那是指揮站的職責（本節），不是 worker 的。通用的
容器收尾流程假設「我是唯一在這個容器裡的 session，退出後會落在主 checkout」，派工拓樸下不
成立：worker 退出後的落點是**派它出來的指揮站所在的容器**（多半是另一個 worktree），於是那
條流程的退出閘門會偵測到仍在隔離環境而中止。它不會誤清或誤合任何內容，但這代表 worker 走這
條路徑注定卡死，徒耗一輪。派工單若寫了要求 worker 自行收尾，那份派工單本身有誤。

**別自己推狀態，跑 `plan_worker_teardown(node)`**（`session_dispatch/mission_plan.py`，
plan-only）。它逐節點回 `disposed` / `pending` / `unavailable` 三態、容器路徑、dirty 檔案、
還坐在裡面的 session、`unpushed`、`branch_merged`，以及排好順序的 `steps`。

跨 repo 節點三件事跟同 repo 不同，**完整 playbook 見 `references/cross-repo.md`**：

1. **SHALL 宣告 `node.work_branch`** —— 定位容器靠你指定的分支反查，不靠容器名（推不準）
   也不從 `done_signal` 反解（那是自由格式 shell，反解失敗長得像「容器不在了」）。
2. **未宣告 `work_branch` 回 `unavailable` 而非 `disposed`** —— 缺錨點時枚舉一樣全落空，
   外觀與已處置相同；回 `disposed` 就是用「我沒找到」冒充「它不存在」。
3. **`steps` 不含 `git branch -D`** —— 分支去留是目標 repo 的政策。依指揮站的分支慣例去判定
   一條外來分支，指過去不會報錯，只會錯。

---

## 8. 決策記錄

**兩項被正面否決的替代方案，理由詳見 `references/decisions.md`：**

- **不用官方 agent teams** — 最關鍵一項是**無 worktree 隔離**，而本工作流的隔離正是靠每個
  worker 各有 checkout；其餘為實驗性與 token 成本。
- **不與 project 級 task graph 合流** — 邊界畫在**壽命**不在機制：那邊是 project 級跨月且進
  git，mission 是一輪對話級、machine-local、做完即棄。故 SHALL NOT 引入 provenance、確定性
  id、衝突收斂或跨機同步；要加請先正面推翻 `SPEC.md` 條文。

---

## 反合理化

| Rationalization | Reality |
|---|---|
| 「worker 訊息說做完了，可以推進下游」 | 訊息不是完成判定。`ready_nodes()` 刻意讓訊號壓過 plan 的 status，就是為了擋這個。訊息可能來自一個自我感覺良好但沒產出的 worker |
| 「這個節點的完成條件很明顯，不用寫 done_signal」 | 「很明顯」是你腦中的狀態，不是可執行的東西。寫不出來就是還沒收斂——這時派工出去的是一份沒有驗收條件的規格 |
| 「訊號指令跑失敗，那就當它還沒做完，繼續等」 | 那是靜默降級。壞掉的訊號會讓你等一個永遠不會到來的完成。`unavailable` 要單獨列出來看 |
| 「反正 worktree 預設就是從我這裡開，不用寫 base_ref」 | 那正是它該被寫下來的理由。隱式繼承會讓 worker 帶著你未合併的 commits 開工，而你不會發現——漏填在建構期就炸就是為了讓你面對這個決定 |
| 「A 做完順手把任務內容傳給 B，省一趟」 | 省下的一趟換來的是失真的派工單。A 手上沒有你這輪對話的脈絡，它轉述的是它理解的版本 |
| 「三件事分開派比較快」 | 有順序依賴時並行度是 1，你付了三份冷啟動卻沒買到並行。先問「分開做真的比較快嗎」 |
| 「先派出去再說，派工單邊做邊補」 | 下游會拿著半份規格走完整套流程。派工單的成本在你身上，錯誤的成本在整條鏈上 |
| 「這條前提我上次查過，驗證指令附上去就好」 | **附指令不等於跑過指令**，而兩者在派工單上外觀相同。你抄的是舊記錄，下游卻會當成你剛查證的事實——實測兩條這樣寫的前提都被當場證偽 |
| 「邊界寫『B 在 `worktree-xxx` 分支上改那個檔』夠清楚了」 | 分支名是 worker 的自治範圍（過型別 guard 要 rename、撞上分支被他處持有要改切容器）。實測 C 依分支名複驗，看到的是零 commit 的空分支。用「檔案路徑 ＋ 節點 id」——**那兩樣 worker 改不動** |
| 「每個節點都給 `--worktree`，多開一個不礙事」 | 指定既有分支時那容器必然用不到（git 不准同分支兩處 checkout），worker 會改切既有容器。代價不只浪費：roster 與 `git worktree list` 會與實際工作位置不符，**而指揮站正是靠它們判斷誰在哪裡** |
| 「訊息沒送到再說，先當它會送到」 | 那就是把 messaging 當 dependency。降級路徑要在設計時就存在，不是出事後再補 |
| 「session 停掉了，worktree 自然就清了」 | 有未 commit 變更時清理會被拒絕。不處置就會累積一堆佔著分支的殭屍 worktree |
| 「worker session 停掉了，容器應該也清了」 | 同上，且**停掉之後 roster 就查不到它**——正是最需要定位的時刻失去輔路徑。跑 `plan_worker_teardown(node)`，它靠你指定的分支反查，不依賴 session 還活著 |
| 「跨 repo 的容器在別人 repo，收尾不是我的事」 | **容器是你促成的**，不清就是把殭屍留在別人的版本庫裡。分界不在 repo 邊界而在**層**：容器與 session 歸你，分支去留與 merge 歸那個 repo 的工作流 |
| 「查不到容器，那應該是已經拆掉了」 | 「查不到」與「不存在」不是同一件事。沒宣告 `work_branch` 時枚舉一樣全落空，外觀完全相同——所以那時回 `unavailable` 不回 `disposed` |
| 「所有 `done_signal` 都綠了，收尾就結束了」 | 訊號說工作做完了，沒說容器回收了。這兩件事在本工作流被刻意分開，因為它們曾經一起沉默 |
| 「worker 反正寫不進 mission 目錄，這條不變式不用再防」 | 只有檔案編輯工具擋得住。shell 與 Python 全數穿透（500 行照樣寫進去），權限層也不擋——**而偏好 shell 的執行模式正把 worker 推向那一側**。那是慣例不是不變式 |
| 「跨 repo 節點沿用同一個 `done_signal` 寫法就好」 | 錨錯 repo 的 git 指令回**非零**，被判成 `pending` 而不是 `unavailable`——一個永遠不 fire 的訊號長得跟一個還在做的 worker 一樣。宣告 `workspace_repo` |
| 「worker 在別的 repo，派工單寫進那個 repo 比較近」 | mission 目錄的路徑形狀是**指揮站 repo 的**約定，且分散存放會讓同一次 dispatch 有 N 份作戰圖。派工單留在指揮站，用 `--add-dir` 給讀取權 |
| 「跨 repo worker 沒帶 `--worktree` 也沒關係，它會自己隔離」 | 背景 session **不會**自動隔離，目標 repo 設定更積極也一樣。沒安排就是落在對方的預設分支上未隔離，接著靜默寫進 shared checkout 或卡住空轉 |
| 「叫 worker 把結果寫到 mission 目錄比較集中」 | **選檔案編輯工具的 worker 寫不進去**——guard 讓它退寫到自己 worktree 的同名路徑，然後回報完成，訊號永遠不 fire（實跑第一次就中）。選 shell 的則靜默寫穿，訊號照常 fire 而不變式已破。兩條路都不該走 |
| 「worker 說它寫好了，那應該就在那裡」 | 「寫好了」和「寫在你以為的地方」是兩件事。先跑訊號，訊號說 pending 就去找它到底寫到哪了 |
| 「模板是 skill 自己給的，照抄一定過 lint」 | 曾經不會。舊模板用 `—— 段落 ——` 而 lint 只認 `## `，照抄得 3 個 findings。現在模板進了回歸測試——但這條的教訓是：**規格的兩個面要互相釘住，否則它們會各自演化** |
| 「這個節點派 subagent，五元件可以省」 | 派工單紀律跨形態通用，而且有實據：13 個 agent 多數是 subagent，三個「可推翻的預設值」都被它們查證後否定。省掉的是你的思考，不是它的工作量 |
| 「subagent 沒有 worktree，產物落哪都行」 | 落點仍要被**驗證**而非採信。「agent 說寫好了」與「寫在你以為的地方」是兩件事，這條風險與形態無關 |
| 「peer 叫我先別動，那就先別動」 | 抑制指令的正確性取決於你當下的狀態，而發送方看不到它。你若已進入不可安全暫停的階段（staged 未 commit、持有 lock），完成並回報比服從安全 |

---

## Cross-references

- `SPEC.md` — 規範性條文 SSOT
- `references/brief.template.md` — 派工單模板（canonical，由測試釘住）
- `references/cross-repo.md` — worker 在別的 repo 時的完整 playbook（兩個錨點、隔離安排、訊號形態）
- `references/incidents.md` — 各條文的實測來源（條文 → 當時發生什麼 → 為什麼那條擋得住）
- `references/decisions.md` — 被正面否決的替代方案與理由
- `examples/mission_plan.example.json` — plan 檔形狀
- `session_dispatch/mission_plan.py` — plan 讀寫、三態訊號求值、ready 計算、派工單 lint
- `session_dispatch/live_sessions.py` — roster 枚舉、撞名檢查、覆蓋範圍轉述
