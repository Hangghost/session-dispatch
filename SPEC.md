# session-dispatch Specification

## Purpose

規範「指揮站派工」工作流：一輪對話結束後，把多個下游任務發給獨立的 background session
或 subagent、追蹤其完成、並收斂回來。涵蓋 mission plan 檔的契約、每節點的完成訊號、
協調拓樸、派工單的品質下限與收斂義務。

存在理由是**這個形狀原本全靠人工**：人工開 session、開完手動改名、人工貼派工單、人工追
進度、人工收斂。而其中最容易流失的不是機械步驟，是**派工單品質**——下游 agent 記憶為零但
檢索極便宜，派工單的字數只該花在它 grep 不到的東西上，寫壞的代價是下游拿著錯誤前提走完
整套流程。

四條貫穿性設計原則：

1. **完成判定的權威是 deterministic 訊號，不是 worker 的自我宣告**——實跑證實 worker 可以
   真心誠意地認為自己完成了，而產物落在別處。
2. **訊息只當 doorbell**，正確性建立在檔案與 git artifact 上，使無 messaging 的環境行為
   逐字一致。
3. **沉默不得被讀成一個具體的決定**——「查不到」與「真的沒有」、「段落缺席」與「段落合格」、
   「訊號跑不起來」與「還沒做完」，各自要有可區分的出口。
4. **mission 是一輪對話級的短命 fan-out**，SHALL NOT 長成 durable、project-scoped 的任務
   依賴圖——那是另一種東西，機制相似而壽命與目的不同。

## Requirements

### Requirement: Mission plan 檔 SHALL 為 machine-local 且僅由指揮站寫入

一次 dispatch 的作戰圖 SHALL 落於 machine-local 的 mission plan 檔，路徑 SHALL 經
`mission_plan` 提供的 accessor（`mission_home()` / `mission_path()` / `brief_path()`）取得，
SHALL NOT 由呼叫方自行拼接。

錨點預設為 main checkout 之下的 `.claude/missions/`，並 SHALL 可經 `SESSION_DISPATCH_HOME`
環境變數覆寫。錨點解析 SHALL 使所有 worktree 收斂到同一目錄——否則同一個 mission 會因為
指揮站當下站在哪個 worktree 而讀到不同的檔案。

plan 檔 SHALL 只由指揮站寫入。被派工的 session（以下稱 worker）SHALL NOT 寫入 plan 檔——單一
寫入者消除並發寫入，且使完成判定不依賴 worker 記得回報。

plan 至少 SHALL 載：mission id、每個節點的 id、指派對象名稱、執行形態、依賴節點、完成訊號、
該節點的 base ref、以及該節點是否需要獨佔主 checkout。

#### Scenario: worker 完成工作但未回寫 plan

- **WHEN** 某 worker 完成其節點並產出宣告的完成訊號，但從未寫入 plan 檔
- **THEN** 指揮站 SHALL 仍能判定該節點完成（依完成訊號），SHALL NOT 因 plan 檔未被更新而
  視為未完成

#### Scenario: 路徑經 canonical accessor 取得

- **WHEN** 任何 production code 需要定位 mission plan 檔
- **THEN** SHALL 呼叫 `mission_plan` 的路徑 accessor，SHALL NOT 以字串拼接組出該路徑

#### Scenario: 指揮站坐在 worktree 內

- **WHEN** 指揮站是 worktree-isolated 的 session，而 mission 目錄錨定在 main checkout
- **THEN** 路徑 accessor SHALL 解析到 main checkout 的同一目錄，SHALL NOT 因指揮站位置不同
  而產生第二份 mission 目錄

### Requirement: 節點 SHALL 宣告執行形態，形態專屬條文 SHALL NOT 跨形態套用

每個節點 SHALL 於 plan 檔宣告其**執行形態**：`session`（獨立 agent session，具名、有自己的
worktree 與分支）或 `subagent`（由指揮站 spawn、與指揮站共用檔案系統與生命週期、結果直接
回到 caller）。未宣告時 SHALL 視為 `session`——預設落在約束較嚴格的一側，使漏填得到的是
完整約束而非豁免。

下列條文**綁定 `session` 形態**，SHALL NOT 套用於 `subagent` 節點，理由是其成立前提（跨
worktree 的寫入隔離、以名稱定址的訊息通道、獨立的分支與 worktree 生命週期）在 subagent
形態下不存在：

- 產物 SHALL NOT 落於 mission 目錄（該條的理由是 worker 受 worktree 隔離）
- `done_signal` 作為完成判定的唯一權威
- session 名稱的撞名先驗
- `base_ref` 的顯式宣告
- star 拓樸的 worker 間訊息限制
- worker worktree 的收尾處置義務

下列條文**跨形態一律適用**，SHALL NOT 因節點形態為 `subagent` 而豁免：

- 派工單 SHALL 落檔並通過五元件檢查表
- 收斂 SHALL 產出合成報告
- **產物落點 SHALL 被驗證而非採信**——實跑證明「agent 回報寫好了」與「寫在指揮站以為的
  地方」是兩件事，此風險與執行形態無關

`subagent` 節點的產物 SHALL 落於指揮站工作目錄下的 mission 子目錄，並 SHALL 於收斂時遷入
留存位置（指揮站工作目錄若為 ephemeral worktree，其內容不會存活至清理之後）。

#### Scenario: 節點未宣告形態

- **WHEN** plan 檔的某個節點不帶形態欄位
- **THEN** SHALL 視為 `session` 形態並套用完整約束，SHALL NOT 因欄位缺席而略過任何條文

#### Scenario: subagent 節點未宣告 done_signal

- **WHEN** 某節點形態為 `subagent` 且未宣告 `done_signal`
- **THEN** SHALL NOT 因此拒絕派工——該條文綁定 session 形態；但其派工單仍 SHALL 通過五元件
  檢查

#### Scenario: subagent 節點的產物落點

- **WHEN** 某 `subagent` 節點產出需要被指揮站讀取的檔案
- **THEN** 落點 SHALL 為指揮站工作目錄下的 mission 子目錄，且指揮站 SHALL 在收斂時驗證該
  檔案確實存在於該處，SHALL NOT 僅依 subagent 的回報認定

### Requirement: 每個 session 節點 SHALL 宣告 deterministic 完成訊號，無法宣告者不得派工

每個 `session` 節點 SHALL 宣告一個 `done_signal`：一條可執行、以退出碼為結論的指令。指揮站
SHALL 以此作為節點完成的**唯一權威判定**。

worker 的自我宣告（訊息、口頭回報）SHALL NOT 作為完成判定依據；它至多作為喚醒指揮站的訊號。

**無法宣告 `done_signal` 的 `session` 節點 SHALL NOT 被派工。** 無法用一行指令描述「怎樣算
做完」代表任務定義尚未收斂，此時正確動作是繼續討論而非派工。

**空的 `done_signal` SHALL NOT 被求值。** shell 對空字串命令回退出碼 0，照常求值會把「未宣告
訊號」判成「已完成」——最糟的失效方向。

#### Scenario: 訊息宣稱完成但完成訊號未出現

- **WHEN** worker 傳訊息宣稱已完成，但其 `done_signal` 指令仍回非零
- **THEN** 指揮站 SHALL 視該節點為未完成，SHALL 呈現兩者不一致供人判斷，SHALL NOT 據訊息
  推進下游節點

#### Scenario: 任務無法宣告完成訊號

- **WHEN** 規劃階段某 `session` 節點無法給出可執行的 `done_signal`
- **THEN** 該節點 SHALL NOT 進入 dispatch，SHALL 退回討論階段並顯式說明原因

#### Scenario: 空訊號不得被判為完成

- **WHEN** 某節點的 `done_signal` 為空字串或僅含空白
- **THEN** 求值 SHALL 回 `pending` 並註明未宣告訊號，SHALL NOT 因 shell 回 0 而判為 `done`

### Requirement: 訊號求值 SHALL 為三態，求值失敗 SHALL NOT 併入未完成

`evaluate_signal()` SHALL 回 `done` / `pending` / `unavailable` 三態。指令本身跑不起來（逾時、
OSError）SHALL 回 `unavailable` 並附原因，SHALL NOT 併入 `pending`。

呈現層 SHALL 單獨列出 `unavailable` 節點，SHALL NOT 把它渲染成任一側——把「這條指令跑不起來」
渲染成「還沒做完」，會讓指揮站永遠等一個不會到來的完成。

`subagent` 節點 SHALL NOT 被求值，且 SHALL NOT 被列為 `unavailable`——它沒有 `done_signal`
是形態的正常結果，不是壞掉的訊號。

#### Scenario: 訊號指令逾時

- **WHEN** 某節點的 `done_signal` 執行逾時
- **THEN** 求值結果 SHALL 為 `unavailable` 並附逾時原因，SHALL NOT 為 `pending`

#### Scenario: subagent 節點的求值結果

- **WHEN** 對含 `subagent` 節點的 mission 執行 `evaluate_all()`
- **THEN** 該節點 SHALL NOT 被執行訊號求值，其結果 SHALL NOT 為 `unavailable`

### Requirement: Worker 產物 SHALL 落於 worker 寫得到的位置，SHALL NOT 落於 mission 目錄

`session` 節點的 `done_signal` 與派工單指定的產出位置 SHALL 是 worker 實際寫得進去的地方
——**worker 自己的 worktree**，或**其分支上的 commit**。SHALL NOT 要求 worker 寫入 mission
目錄。

理由是 worker 同樣受 worktree 隔離約束，對自身 worktree 之外的寫入會被擋下；被要求寫入
mission 目錄的 worker 會改寫到自己 worktree 內的同名相對路徑，於是回報「完成」而完成訊號
永不 fire。此限制同時**免費強制了「plan 只由指揮站寫」**這條不變式，故 SHALL NOT 試圖繞過。

產物需活過 worker worktree 清理時，`done_signal` SHALL 採 commit 形態；一次性回報可落
worktree 內檔案，此時指揮站 SHALL 在收尾清理前把要保留的內容讀走。

#### Scenario: 派工單要求 worker 寫入 mission 目錄

- **WHEN** 某 `session` 節點的 `done_signal` 指向 mission 目錄下的檔案
- **THEN** 該節點 SHALL 被視為設計錯誤並改寫落點，SHALL NOT 派工

#### Scenario: 產物需活過 worktree 清理

- **WHEN** 某節點的產出需要在 mission 收尾後仍可取得
- **THEN** 其 `done_signal` SHALL 採 commit 形態，SHALL NOT 只落 worktree 內檔案

### Requirement: 協調拓樸 SHALL 為 star，worker 間訊息 SHALL 僅含節點指針

所有 worker SHALL 只與指揮站通訊。worker 之間 SHALL NOT 互傳任務定義、實作細節或決策依據。

允許 worker 於完成後直接向下游節點的 session 送訊息作為 latency 優化，但該訊息內容 SHALL
僅含 mission id 與節點 id（doorbell 語意），SHALL NOT 承載任務定義。任務定義的唯一來源
SHALL 是指揮站產出的派工單。

理由 SHALL 記載於 skill：任務定義誕生於指揮站的對話脈絡，該脈絡是下游 grep 不到的資訊，經
worker 轉述必然失真並夾帶轉述者自身的實作細節。

#### Scenario: worker 試圖轉述任務給下游

- **WHEN** 某 worker 完成節點 A 並準備通知節點 B
- **THEN** 其訊息 SHALL 僅含 mission id 與節點 id，B SHALL 從指揮站產出的派工單取得任務定義

### Requirement: 抑制類訊息 SHALL 宣告自身失效條件，接收方以自身狀態為準

**抑制類訊息**（要求接收方暫停、延後或不要執行某動作的訊息）SHALL 宣告自身的失效條件，形如
「若你已進入 X 階段則本訊息作廢」。無條件的抑制祈使句 SHALL NOT 被送出。

失效條件 SHALL 置於訊息開頭，使接收方在讀到請求內容之前即可判斷是否忽略。

接收方 SHALL 以自身實際狀態為準，SHALL NOT 因收到抑制指令而中斷一個已進入**不可安全暫停
階段**的操作。「不可安全暫停」的判準是：**暫停會把一個半完成的狀態留在別人也要用的資源上**
——staged 未 commit、持有 lock、寫到一半的檔案皆屬之。

理由與「worker 的自我宣告不算完成」同構：抑制指令是一個關於**接收方狀態**的斷言，而發送方
對那個狀態沒有觀測權。兩者都 SHALL 讓真相層（接收方的實際狀態）壓過訊息。

抑制類訊息 SHALL NOT 被當成互斥機制。需要真正互斥時 SHALL 使用 lock 或 lease。

#### Scenario: 抑制訊息抵達一個 mid-operation 的接收方

- **WHEN** 接收方已完成 staging 但尚未 commit，此時收到「先別執行」的抑制訊息
- **THEN** 接收方 SHALL 完成該操作至可安全暫停的狀態並回報，SHALL NOT 因服從訊息而把半完成
  狀態留在共用資源上

#### Scenario: 抑制訊息未附失效條件

- **WHEN** 某 session 準備送出無條件的「先別做 X」訊息
- **THEN** 該訊息 SHALL 補上失效條件後再送出，SHALL NOT 以無條件祈使句形式送出

### Requirement: 派工單 SHALL 落檔，且 SHALL 通過五元件檢查表

派工單 SHALL 寫入檔案，開 session 的 seed prompt SHALL 僅含該檔案的絕對路徑與「讀它並依其
執行」的指示，SHALL NOT 把派工單全文塞進命令列參數。

派工單 SHALL 通過下列五項檢查，缺任一項 SHALL NOT 送出：

1. **已查證的前提附帶驗證動作** — 每條要求下游採信的斷言，SHALL 附一條成本近乎零的複查指令
2. **線索與指令分開標示** — 開放性線索 SHALL 顯式標明「不是指令」，避免被當成祈使句執行
3. **判斷寫成可推翻的預設值** — 指揮站的傾向 SHALL 附理由並顯式授權下游推翻
4. **邊界描述誰握著什麼** — SHALL 寫並行 session、分支佔用、不可動的檔案，SHALL NOT 以限制
   解法代替
5. **每個回報項附用途** — SHALL 說明該回報要拿去做什麼，使下游知道該答到多細

檢查 SHALL 針對可觀察的證據（有無驗證指令、有無標示、有無用途說明），SHALL NOT 退化為「有無
對應標題」。

結構性遺漏 SHALL 由機械檢查兜底（`lint_brief()`），使「整段忘了寫」從靠自律變為會紅燈。該
檢查 SHALL 涵蓋**段落缺席**與**段落存在但內容缺項**兩種形態，且兩者 SHALL 給出可區分的
finding 文字——使用者的下一步動作不同（前者新增段落，後者補內容），把兩者壓成同一個出口
（或把缺席壓成沉默通過）即是靜默降級。

機械檢查 SHALL NOT 綁定單一段落標記語法。段落標記屬呈現細節，因使用者採用模板以外的等價
標記而報缺段，製造的是假陽性；檢查器 SHALL 對格式寬容、對內容嚴格。**skill 所示範的派工單
模板 SHALL 逐字通過本檢查**——模板與檢查器是同一份規格的兩個面，兩者不一致時使用者無從判斷
該信哪個。

但該檢查 SHALL NOT 被當成派工單品質的充分條件——它抓得到結構缺席，抓不到內容空洞（一個寫著
「（我要拿去參考）」的回報項會通過），語意品質仍是人的判斷。呈現層 SHALL NOT 把 lint 通過
描述為派工單已合格。

#### Scenario: 前提未附驗證動作

- **WHEN** 派工單含「已確認此模組的併發模型安全」但未附任何複查路徑
- **THEN** 該派工單 SHALL NOT 送出，SHALL 補上驗證指令或改寫為線索

#### Scenario: 元件段落整段缺席

- **WHEN** 派工單完全沒有「已查證的前提」段或「線索」段
- **THEN** `lint_brief()` SHALL 回報該元件缺席，SHALL NOT 因該段不存在而略過檢查並沉默通過

#### Scenario: 派工單採用等價的段落標記

- **WHEN** 派工單以 `—— 邊界 ——` 而非 `## 邊界` 標記段落，內容具備該元件的可觀察證據
- **THEN** `lint_brief()` SHALL 辨識該段落，SHALL NOT 回報缺段

#### Scenario: 模板逐字通過檢查

- **WHEN** 把 skill 所示範的派工單模板逐字餵進 `lint_brief()`
- **THEN** SHALL 回報零 findings

#### Scenario: lint 通過不等於派工單合格

- **WHEN** 一份結構完整但內容空洞的派工單通過 `lint_brief()`
- **THEN** 呈現層 SHALL NOT 宣稱該派工單已合格，人工檢查表仍 SHALL 被執行

### Requirement: Dispatch SHALL 於單一確認點呈現完整計畫，session 名稱 SHALL 先驗不撞

指揮站 SHALL 在開啟任何 worker session 之前，一次呈現完整計畫：節點清單、session 名稱、
形態、model、完成訊號、依賴關係、以及哪些節點需要獨佔主 checkout；並取得**單次**確認。
SHALL NOT 逐節點多輪問答。

session 名稱即定址，故 dispatch 前 SHALL 以本機 roster 驗證擬用名稱未與現存 session 重複；
重複時 SHALL 改名，SHALL NOT 依賴列表的消歧後綴。

撞名檢查 SHALL 經 `name_is_taken()` 進行，SHALL NOT 手刻 CLI 輸出解析。理由有二：roster 的
欄位形狀是外部 CLI 的非正式契約；且**在非 TTY 環境下該 CLI 根本沒有輸出可解析**，手刻的
管線會安靜落空，而落空看起來與「沒撞名」完全相同。

預設 SHALL 為每個節點開啟新 session。復用既有 idle session SHALL 由使用者顯式指定，且指揮站
SHALL 先呈現該 session 的 cwd 與所在分支。

#### Scenario: 擬用名稱與現存 session 重複

- **WHEN** 規劃的 session 名稱已存在於本機 roster
- **THEN** 指揮站 SHALL 改用不重複的名稱，SHALL NOT 直接開啟同名 session

#### Scenario: roster 查詢失敗

- **WHEN** 撞名檢查所依賴的 CLI 不可用或非零 exit
- **THEN** roster SHALL 回報 `available=False`，SHALL NOT 回 `available=True` 的空清單；呼叫
  方 SHALL NOT 因此 block 派工（fail-soft），但 SHALL 能區分「沒撞」與「查不到」

#### Scenario: 一次確認而非逐項詢問

- **WHEN** mission 含三個節點
- **THEN** 指揮站 SHALL 以一次呈現 + 一次確認完成授權，SHALL NOT 為每個節點各問一次

### Requirement: Worker 的 base ref SHALL 顯式宣告，SHALL NOT 沿用隱式繼承的 HEAD

每個 `session` 節點 SHALL 於 plan 與派工單中顯式宣告 `base_ref`，且派工單 SHALL 指示 worker
的第一個動作為從該 base 開出自己的分支，SHALL NOT 沿用 worktree 建立時繼承的 HEAD。指揮站的
確認畫面 SHALL 顯示每個節點的 base ref，使其成為被看過的決定而非預設值。

存在理由：以隔離 worktree 開啟的 worker session，其 worktree 的 base 常為**啟動方（指揮站）
當下的 HEAD**，而非主線。指揮站通常正坐在自己的工作分支上，因此 worker 會隱式繼承指揮站
分支的 commits。

繼承指揮站的 HEAD 本身 SHALL NOT 被禁止——當節點的工作正是建立在指揮站尚未合併的變更之上時，
那是正確選擇。本 requirement 約束的是它 SHALL 被顯式選擇。漏填 SHALL 於建構期即失敗。

#### Scenario: 指揮站坐在工作分支上派工

- **WHEN** 指揮站於 `feature/X` 開啟一個與 `feature/X` 無關的節點
- **THEN** 該節點的 `base_ref` SHALL 被顯式宣告，派工單 SHALL 指示 worker 從該 base 開分支

#### Scenario: 節點刻意建立在指揮站的未合併變更之上

- **WHEN** 某節點的工作依賴指揮站分支上尚未合併的 commits
- **THEN** 其 `base_ref` SHALL 顯式宣告為該分支，SHALL NOT 因「反正預設就是繼承」而略去宣告

### Requirement: Mission 收尾 SHALL 對每個 worker worktree 做出處置決定

mission 收尾 SHALL 對每個 worker worktree 給出明確處置（提交／捨棄／保留待查），SHALL NOT
假設停止 session 即完成回收。

理由是 worker session 停止後其 worktree **不會**自動回收：目標 worktree 若存在未 commit
變更，清理指令會拒絕執行並保留該 worktree。完成訊號與產物落地是兩件事——訊號說工作做完了，
未 commit 變更說產物還沒落地。

#### Scenario: worker worktree 留有未 commit 變更

- **WHEN** mission 收尾時某 worker 的 worktree 仍有未 commit 變更
- **THEN** 收斂流程 SHALL 呈現該事實並要求處置決定，SHALL NOT 靜默略過或宣稱已清理

### Requirement: 無 messaging 環境下的行為 SHALL 與導入前逐字一致

工作流的正確性 SHALL 只建立在 plan 檔與完成訊號上。cross-session messaging SHALL 僅作為縮短
latency 的喚醒通道。

在 messaging 不可用的環境（低於門檻版本的 CLI、native Windows、其他 agent harness、功能被
設定關閉）中，工作流 SHALL 仍可完整執行——指揮站以完成訊號輪詢取代訊息喚醒，結果 SHALL 與有
messaging 時相同，差別 SHALL 僅在延遲。

指揮站 SHALL NOT 設定節點等待逾時，SHALL NOT 於等待逾時後升級為人工確認；SHALL 改為呈現該
節點已等待時長與該 session 於 roster 中的狀態。

#### Scenario: 訊息未送達仍能推進

- **WHEN** worker 完成節點且產出完成訊號，但其通知訊息因接收端設定被拒或過期而未送達
- **THEN** 指揮站 SHALL 仍能經完成訊號偵測到完成並推進下游節點

#### Scenario: 長時間無完成訊號

- **WHEN** 某節點已派工且長時間未出現完成訊號
- **THEN** 指揮站 SHALL 呈現等待時長與該 session 的 roster 狀態（含 pid），SHALL NOT 自動
  abort 該節點

### Requirement: 需獨佔主 checkout 的節點 SHALL 被標記且不並排

節點若需在主 checkout 獨佔執行（典型為跨分支 merge、需要全 repo 視角的清理），SHALL 於 plan
中標記。指揮站 SHALL NOT 同時派出兩個此類節點，且該類節點的派工單 SHALL 說明可能撞上誰與
對應的等待協定。

#### Scenario: 兩個節點皆需獨佔主 checkout

- **WHEN** mission 含兩個標記為需獨佔主 checkout 的節點
- **THEN** 指揮站 SHALL 使其循序執行，SHALL NOT 同時派出

### Requirement: Mission 結束 SHALL 產出合成報告

mission 的所有節點終止（完成或放棄）後，指揮站 SHALL 產出一份合成報告落檔，載明各節點的
產出、被否決的替代方案、以及**跨節點的結論**。

存在理由是派工單的回報項構成承諾：列出回報要求卻不消化，下一次下游便無理由認真回報。

跨節點結論 SHALL 被單獨呈現，SHALL NOT 只是各節點報告的串接——實跑出現過多個互不知情的節點
各自指向同一批高風險項，該訊號只在合成階段可見。

#### Scenario: 節點全數終止後的收斂

- **WHEN** mission 的所有節點已完成或被放棄
- **THEN** 指揮站 SHALL 產出合成報告，SHALL NOT 僅在對話中口頭總結後即結束 mission

### Requirement: 本能力 SHALL NOT 長成 durable project-scoped task graph

mission 的語意 SHALL 限定為**一輪對話級的短命 fan-out**：machine-local、做完即棄。本能力
SHALL NOT 引入依賴邊的阻塞語意推導、provenance、確定性 id、衝突收斂或跨機同步。

那些屬於 project 級、跨月存活、進 git、可視化的任務依賴圖——機制相似而壽命與目的不同，把
本能力長成那個會得到一個兩邊都做不好的中間物。

任何擴充本能力使其持久化、跨機、或承載專案級任務圖的提案，SHALL 先顯式推翻本條文，SHALL NOT
以漸進增補的方式繞過。

#### Scenario: 提案要求 mission 持久化跨機

- **WHEN** 有人提案讓 mission plan 進 git 以便跨機檢視
- **THEN** 該提案 SHALL 先正面處理本條文，SHALL NOT 逕行實作

### Requirement: 本能力 SHALL 由一份 skill 記錄，且該 skill SHALL 自帶判準

本能力的操作知識 SHALL 落於一份 skill 文件，內含：派工與否的判準、mission 規劃步驟、節點
形態的適用邊界、開 session 指令形態、派工單五元件檢查表與模板、star 拓樸規約、收斂義務、
以及反合理化表。

skill SHALL **自帶**「這個任務該不該開獨立 session」的四軸判準與三項隱性成本，SHALL NOT 僅
放指針——本能力是可獨立安裝的 skill，指向使用者 repo 內某份不存在的文件等同沒有判準。

skill SHALL 額外記載兩項專屬判準與決策：**(a)** 派工前先評估「這幾件事分開做是否真的較快」
——冷啟動成本隨 session 數線性增長，且獨立視角與並行度是兩條各自成立的理由；**(b)** 不採用
官方 agent teams 的理由，其中 SHALL 含「agent teams 無 worktree 隔離，而本工作流的隔離正
依賴每個 worker 各有 checkout」。

#### Scenario: 日後有人詢問為何不用 agent teams

- **WHEN** 需要判斷本工作流是否該改用官方 agent teams
- **THEN** skill SHALL 已載明該決定與理由，SHALL NOT 需要重新推導

#### Scenario: skill 被安裝到本 repo 之外

- **WHEN** 使用者把本 skill 安裝到自己的 repo
- **THEN** skill 內的判準 SHALL 完整可讀，SHALL NOT 依賴任何本 repo 以外的文件
