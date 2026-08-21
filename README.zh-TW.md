# session-dispatch

**一套將「向獨立 Agent Session 派發下游任務，並精確收回成果」模組化的 Skill。**

> 🇬🇧 **English: [README.md](README.md)**

背景開幾個獨立的 Session，其實一行指令就能完成。本 Skill 要解決的是開完 Session *之後* 才開始出現的四個根本難題：

- **你怎麼知道一個節點真的做完了？**（不是靠去問它）
- **交給 Worker 的派工單 (Brief) 裡面該放什麼、不該放什麼？**（給座標，而不是給知識）
- **Worker 之間可以相互溝通嗎？**（可以敲門示意，但不能傳遞任務定義）
- **當工作收回時會發生什麼事？**（列了五項要求回報，最後卻只掃一眼結論——下次就再也沒人會認真回報了）

## 為什麼「完成訊號」是整個機制的核心？

在第一次真實測試時就踩到了這個坑：一個 Worker 發訊息說「做完了 (*done*)」，計畫狀態顯示為 *pending*，而產出檔案**確實存在——只是被放在 Worker 自己的 Git worktree 裡**。

它並沒有說謊，它真心認為自己完成任務了。產出物只是落在了發派端 (Dispatcher) 看不到的地方而已。

因此，每個節點都必須宣告一個 `done_signal`：這是一條 Shell 指令，**唯一的**完成判定標準就是該指令的 Exit code 為 0。

```python
from session_dispatch import MissionNode

MissionNode(
    id="audit",
    session_name="2026-08-15-cleanup-audit",
    brief_path="/abs/path/to/audit.brief.md",
    done_signal="git log --oneline worktree-...-audit | grep -q 'fix(inbox)'",
    base_ref="main",
)
```

**如果你寫不出這條判定指令，這個節點就不該被派發出去。** 如果你無法用一條指令明確定義「怎樣才算做完」，代表任務定義根本還沒收斂。這條規則所帶來的副作用比規則本身更有價值：它會強迫你在把工作交出去*之前*，就必須先把驗收條件 (Acceptance Criteria) 想清楚。

狀態評估會回傳**三種**狀態，其中 `unavailable`（代表指令本身無法執行）被刻意排除在 `pending` 之外。如果把壞掉的訊號誤判為「還在執行中」，發派端就會陷入無窮等待，去等一個永遠不會到來的完成訊號。

## 派工單 (Brief) 的五大核心元件

Agent 的 Context 擁有兩個與人類完全相反的特性：

- **零記憶。** 下游的 Session 對你剛剛花半小時思考的脈絡一無所知。你腦中的一切——「我已經確認過不是 A」、「我試過路徑 B」——對它來說完全不存在。
- **檢索成本極低。** 但它可以在三秒內 grep 完整個 Codebase 並讀完五個檔案，輕鬆驗證任何推測。

對人類同事來說恰恰相反：人類擁有累積的記憶，但重新檢索資料的成本很高。所以你會直接給人類結論。但給 Agent 結論反而是一種浪費——結論是它自己就能低成本產出的東西，而你給的結論還帶有「可能會錯」的風險。

**字數應該只花在下游 Agent「無法自己查到」的事情上。**

| # | 元件 | 可觀察的證據 |
|---|---|---|
| 1 | 已查證的前提需附帶驗證動作 | 任何希望下游採信的主意或主張，都應附上一條執行成本接近零的複查指令 |
| 2 | 線索與指令必須明確劃分 | 開放性的討論區段需顯式標註「這不是指令」 |
| 3 | 將判斷寫成「可被推翻的預設值」 | 附上傾向 + 推理過程 + 顯式授權下游推翻此結論 |
| 4 | 明確界定資源與邊界權屬 | 列出並行 Session、分支擁有權、禁止動用的檔案區段 |
| 5 | 每個要求回報的項目都要說明用途 | 說明收回這個答案後你要拿去做什麼 |

第 3 點是最反直覺、但回報率最高的一點。在一次動用 13 個 Agent 的任務中，派工單裡寫了三個「我傾向這樣做，但你可以推翻我」的判斷——**結果三個全被下游推翻了**。給出帶有推理過程的預設值，等於提供給 Agent 一個可以低成本推倒的標靶；若只寫「請自行判斷」，Agent 就得從零建構評估標準，最後只會回報現狀，而不是提出實質質疑。

### 機械化的底線檢查 (Linter)

```bash
python3 -c "from session_dispatch import lint_brief; import sys; print(lint_brief(open(sys.argv[1]).read()))" brief.md
```

`lint_brief()` 用來捕捉**結構性遺漏**——例如整段缺席、前提段落沒有附驗證指令、或是回報項目沒寫明用途。段落完全缺失與段落存在但內容不完整，會產生*不同*的提示訊息，因為你下一步的動作不同（補充缺少的段落 vs. 填滿既有段落）。

但它**不會**檢查內容是否實質為空——例如將回報用途寫成 `(作為參考)` 也會通過測試。因此**通過檢查（綠燈）不代表這是一份合格的派工單**：綠燈僅代表結構*存在*，無法保證內容*正確*。

## 節點可以是獨立 Session，也可以是 Subagent

並不是每個節點都需要單獨開一個 Session。`MissionNode.shape` 用來區分這兩種形態，而**形態決定了適用哪些規範**：

| 規範項目 | `session` | `subagent` |
|---|---|---|
| 派工單五大元件、驗證產物落點路徑、收斂總結報告 | ✅ | ✅ |
| `done_signal`、`base_ref`、名稱衝突檢查、worktree 清理處置 | ✅ | ❌ |

Subagent 沒有獨立的 worktree，也不會跨 Channel 回報——上述規則的前提在 Subagent 身上根本不存在，硬套上去只會製造無謂的儀式感，無法提供實質保證。但是**派工單的書寫紀律在兩者間完全通用**，這一點有實據支持：前述三個被推翻的預設判斷，全都是被 Subagent 所推翻的。

預設形態為 `session`：當欄位漏填時，會套用最嚴格的全套約束，而不是給予豁免。

## 把 worker 派到另一個 repo

worker 的 workspace 可以是**另一個 git repository**——這個 repo 當指揮站，worker 在別的 repo
裡跑那個 repo 自己的工作流。三件事會不一樣：

- **完成訊號改為逐節點求值**，錨定在 `MissionNode.workspace_repo`，不是一個 mission 共用的
  cwd。錨錯 repo 的 git 訊號會回非零而被讀成「還在做」——所以 repo 不可達時回 `unavailable`，
  絕不回 `pending`。
- **隔離必須顯式安排。** 背景 session **不會**自動隔離，兩個 repo 皆然。要嘛啟動時就指定容器，
  要嘛把「進入容器」寫成 worker 的第一個動作。
- **plan 與派工單留在指揮站**，worker 靠顯式的目錄授權讀取。跨 repo 的訊號只能走 commit 形態：
  對方工作流會挑什麼容器名，你推不準。

`dispatch_plan(node, mission_id)` 會把這些事實算出來——launch 目錄、要授權哪些目錄、派工單
絕對路徑、容器由誰建立——而且**不開 session、不建任何東西**。

## 收尾：worker 容器的處置

worker 的 worktree 不會因為 session 停掉就被回收。清理會在**兩種**條件下被拒絕：有未 commit
變更，以及**分支上有從未 push 過的 commit**——後者在 squash 流程下結構上必然發生，因為內容
早就進了主線，只是那些 SHA 沒被 push 過。

`plan_worker_teardown(node)` 逐節點回報 `disposed` / `pending` / `unavailable` 三態，外加容器
路徑、dirty 檔案、還坐在裡面的 session，以及排好順序的 `steps`。它是 plan-only——不拆任何東西。

三態的分野是重點：`disposed` 是一句**正面斷言**（「我以可信錨點枚舉過目標 repo，沒有任何容器
屬於這個節點」）。缺錨點時枚舉會以**完全相同的外觀**落空，所以那種情況回 `unavailable`——回
`disposed` 就是用「我沒找到」冒充「它不存在」。

`steps` 永遠不含刪分支。分支政策屬於目標 repo 的工作流，而用這個 repo 的慣例去判定一條外來
分支不會報錯，只會錯。

## 安裝方式

Skill 的核心指令與邏輯定義在 `SKILL.md`，安裝方式取決於你使用的 Agent 工具：

```bash
# Claude Code — 建立 symlink 到 ~/.claude/skills/
ln -s "$(pwd)" ~/.claude/skills/session-dispatch

# 或透過 npx skills（跨 Agent 通用）
npx skills add "$(pwd)" -g
```

Python 底層邏輯零外部套件依賴，完全使用標準函式庫：

```bash
pip install -e .          # 或直接將 session_dispatch/ 目錄複製進你的專案中
pytest                    # 包含 68 個測試案例
```

### 版號與發布

發布以 annotated git tag 標記，格式 `vMAJOR.MINOR.PATCH`，只打在 `main` 上。目前仍在
`0.x`，**minor 進位可能帶 breaking change**——那類變更會在 tag 訊息中以 `BREAKING:`
開頭的行載明，並寫出舊呼叫與新呼叫的形狀。

建議釘版本，不要跟著 `main` 走：

```bash
git tag -n1               # 列出所有版本，最新的在最後

# skill（symlink 安裝）——在你的 clone 內切到該 tag
git checkout v<VERSION>

# Python 套件
pip install "session-dispatch @ git+https://github.com/Hangghost/session-dispatch@v<VERSION>"
```

（範例刻意不寫死版號：README 裡的一個具體版號是**又一份**每次發布都會變的手抄本，
而它過期時不會有任何訊號。）

**本專案刻意不維護 `CHANGELOG.md`**：annotated tag 的訊息就是發布記錄本體，只有一份
需要維持誠實。三份手維護的同一事實會各自漂移。

```bash
git tag -n99              # 列出所有版本與完整訊息
git log v0.1.0..v0.2.0    # 兩版之間的變更
```

維護者：發布流程見 [`RELEASING.md`](RELEASING.md)。

## Mission 檔案的存放位置

預設存放在主專案 (Main checkout) 底下的 `.claude/missions/` 目錄；可透過環境變數 `SESSION_DISPATCH_HOME` 進行覆寫。路徑解析機制會讓**所有 Git worktree 自動收斂到同一個目錄**——否則同一個任務在不同 worktree 下執行時，會因為當前工作目錄不同而讀取到不同的檔案。

這些都是過程產物，建議加入 `.gitignore` 中：

```gitignore
.claude/missions/
```

## 什麼時候**不要**使用本 Skill？

使用這套工作流是有明確成本的：

- **每個 Worker 都需要承受一次冷啟動 (Cold start) 成本**——包含重新載入專案 Context 與規範。三個節點就是三次冷啟動。如果節點之間存在順序依賴，並行度 (Parallelism) 實際上為 1：你付出了三次冷啟動的代價，卻沒有換到任何並行優勢。**這種情況下，自己按順序做完通常還比較快。**
- **N 個並行 Session ＝ N 倍的 API 額度 (Quota) 消耗。**
- **最後的結果收斂責任依然落在你身上。**

但「速度」並不是這套方法值得投資的唯一理由。在五次實際執行的任務中，有三次的並行度其實只有 1——但沒有一次讓人後悔，因為獲得的回報並不是速度。有兩個 Worker 在審查我剛寫好的程式碼時，抓出了一個在我自己的 grep 搜尋條件之外遺漏的程式片段，並質疑了我對自身模組能力歸因的假設，直接促成了 API 設計的改進。

因此，判斷是否使用的基準有兩條，且各自獨立成立：

1. **這些事情分開做會不會比較快？**（僅在「並行度 > 1」時成立）
2. **這件事我自己做會不會存在系統性盲區？**（提供獨立視角——與並行度無關）

第一條不成立，並不影響第二條持續發揮價值。**Code Review、驗證、找自己的錯誤**完全屬於第二條：它們的價值來自於「用另一顆大腦思考」，這跟是不是同時執行完全無關。

## 文件說明

| 檔案 | 內容說明 |
|---|---|
| `SKILL.md` | 完整工作流：包含規劃、派工單撰寫、發派機制、星狀拓樸 (Star topology)、降級處理、結果收斂與反合理化對照表 |
| `SPEC.md` | 25 條規範性條文（SHALL / SHALL NOT），每條均附帶可測試的情境 (Scenarios) |
| `references/brief.template.md` | 派工單模板（標準版本，由單元測試釘住並直接餵給 Linter 驗證） |
| `references/cross-repo.md` | 把 worker 派到**另一個 repo** 時的完整 playbook |
| `references/incidents.md` | 每條規約的實測來源——當時發生什麼、為什麼那條擋得住 |
| `references/decisions.md` | 被正面否決的替代方案與理由 |
| `examples/mission_plan.example.json` | Mission plan 的 JSON 格式範例 |
| `RELEASING.md` | 發布慣例——tag 格式、0.x 版號語意，以及其中哪一半由測試強制 |

`SPEC.md` 是這套工具中最難被複製的核心價值：它把「為什麼不能輕信 Worker 說自己做完了」轉化為可被稽核的具體條文，而不是一段模糊的文字建議。

> **備註：** `SKILL.md` 與 `SPEC.md` 目前以繁體中文撰寫，中文讀者可直接讀本體、不需要轉譯。[英文版 README](README.md) 是給非中文讀者的門面，Skill 本體的英文化尚未進行——若你需要，歡迎開啟 Issue。

## 專案來源與背景

本工具是從個人 AI Agent 知識基礎設施中抽取出來的精華，歷經四天、五次真實任務的實際運作（約包含 9 個背景 Session Worker 與 13 個 Subagent）。文件中凡是標註「實跑」或「實際測試」的地方，都是指這些真實經歷，而非虛構的假設情境。

這其中也包含失敗的教訓。有兩個特別值得列出，因為它們的失敗形態正是這套工作流要防止的對象：

- 一個 Worker 將產出寫到了發派端 (Dispatcher) 視線之外的地方，然後向發派端回報執行成功。
- 派工單模板本身竟然無法通過自己的 Linter 檢查；而 Linter 最重要的一項檢查——「你漏掉了一整個段落」——卻完全沒有觸發，因為當時程式碼被寫成了「*如果該段落存在，才進行檢查*」。

**「將沉默（未檢查/無回應）解讀為某種特定的決策」**——這正是最致命的失效模式，而我甚至把它寫進了用來捕捉這種錯誤的工具裡。

## License

MIT
