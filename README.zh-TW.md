# session-dispatch（繁體中文）

> 🇬🇧 **English: [README.md](README.md)** ← 首頁版本

把「一輪討論結束 → 發出 N 個下游任務 → 收回來」變成可重複執行的工作流。

它解的不是「怎麼開多個 session」——那件事一行指令就做完了。它解的是開完之後的四個問題：

- **怎麼知道一個節點真的做完了？**（不是靠它說）
- **派工單該寫什麼、不該寫什麼？**（給座標，不要給知識）
- **worker 之間可以講話嗎？**（可以敲門，不能傳任務定義）
- **收回來之後呢？**（列了五項回報卻只掃一眼結論，下次就沒人認真回報了）

## 為什麼「完成訊號」是核心

第一次實跑就撞到這個：一個 worker 傳訊說「done」，plan 說 pending，而檔案**確實存在
——在它自己的 worktree 裡**。它沒有說謊，它真心認為自己做完了，只是產物落在指揮站看
不到的地方。

所以每個節點都要宣告一條 `done_signal`：一條 shell 指令，退出碼 0 才算完成。

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

宣告不出這條指令的節點**不該被派工**——那代表任務定義還沒收斂。這條規則的副作用比它
本身更有價值：它逼你在派工前把驗收條件想清楚。

求值回三態，`unavailable`（指令跑不起來）刻意不併進 `pending`。把壞掉的訊號渲染成
「還在跑」，指揮站會永遠等一個不會到來的完成。

## 派工單的五元件

agent 的 context 有兩個跟人相反的性質：**記憶為零**（它不知道你剛才半小時在想什麼），
**檢索極便宜**（它三秒能 grep 完整個 repo）。所以派工單的字數只該花在下游查不到的東西上。

| # | 元件 | 可觀察的證據 |
|---|---|---|
| 1 | 已查證的前提附驗證動作 | 每條要下游採信的斷言，附一條成本近零的複查指令 |
| 2 | 線索與指令分開標示 | 開放性線索段落顯式標明「不是指令」 |
| 3 | 判斷寫成可推翻的預設值 | 傾向 + 理由 + 顯式授權推翻 |
| 4 | 邊界描述誰握著什麼 | 列並行 session、分支佔用、不可動的檔案 |
| 5 | 每個回報項附用途 | 說明該回報拿去做什麼 |

第 3 條在實跑中最反直覺也最有效：一次 13 個 agent 的 mission 裡，**三個寫進派工單的
傾向全被查證後否定**。帶理由的預設值比「你自己判斷」更容易換到真相，因為它給了下游
一個可以反駁的靶。

機械下限：

```bash
python3 -c "from session_dispatch import lint_brief; import sys; print(lint_brief(open(sys.argv[1]).read()))" brief.md
```

`lint_brief()` 抓結構性遺漏（整段缺席、缺驗證指令、回報項沒寫用途），抓不到內容空洞
——「（我要拿去參考）」會通過。**綠燈不等於合格**，它只是把最常見的失效形態從靠自律
變成會紅燈。

## 安裝

skill 本體是 `SKILL.md`，安裝方式取決於你的 agent：

```bash
# Claude Code：symlink 到 ~/.claude/skills/
ln -s "$(pwd)" ~/.claude/skills/session-dispatch

# 或透過 npx skills（跨 agent）
npx skills add "$(pwd)" -g
```

Python 原語零依賴、只用標準函式庫：

```bash
pip install -e .          # 或直接把 session_dispatch/ 複製進你的專案
pytest                    # 45 個測試
```

## mission 檔落在哪

預設是 main checkout 之下的 `.claude/missions/`，可用 `SESSION_DISPATCH_HOME` 覆寫。
路徑解析讓**所有 worktree 收斂到同一個目錄**——否則同一個 mission 會因為你當下站在哪
個 worktree 而讀到不同的檔案。

這些是過程產物，建議加進 `.gitignore`：

```gitignore
.claude/missions/
```

## 節點可以是 session，也可以是 subagent

不是每個節點都值得開一個獨立 session。`MissionNode.shape` 分兩種形態，而**形態決定
哪些規約適用**：

| 規約 | `session` | `subagent` |
|---|---|---|
| 派工單五元件、產物落點要驗證、收斂報告 | ✅ | ✅ |
| `done_signal`、`base_ref`、撞名檢查、worktree 處置 | ✅ | ❌ |

subagent 沒有獨立 worktree、也不隔著通道回報，那幾條的前提不存在——套上去只會製造
儀式，不產生保證。但**派工單紀律跨形態通用**，這點有實據。

預設是 `session`：漏填時得到的是完整約束，不是豁免。

## 什麼時候**不要**用

這套東西有成本，而且不小：

- **每個 worker 都要重跑一次冷啟動。** 三個節點就是三份。節點之間若有順序依賴，並行度
  是 1——你付了三份冷啟動卻沒買到任何並行。**串著自己做完常常比較快。**
- **N 個並行 session ＝ N 倍 quota。**
- **收斂責任回到你身上。**

但速度不是唯一的划算理由。第一次實跑的並行度就是 1，純以速度論自己做更快——可是兩個
worker 抓到的東西不是我會自己想到的（一個在我的 grep 判準射程外，一個質疑了我對自己
模組的能力歸因）。

所以判準有兩條，各自獨立成立：

1. **這幾件事分開做會不會比較快？**（並行度 > 1 才成立）
2. **這件事我自己做會不會有系統性盲區？**（獨立視角，與並行度無關）

**審查、驗證、找自己的錯**屬於第 2 條——它們的價值來自「不是同一顆腦袋」。

## 文件

| 檔案 | 內容 |
|---|---|
| `SKILL.md` | 完整工作流：規劃、派工單、dispatch、star 拓樸、降級、收斂、反合理化表 |
| `SPEC.md` | 15 條規範性條文（SHALL / SHALL NOT），每條附可測試的 scenario |
| `references/brief.template.md` | 派工單模板（canonical，由測試釘住逐字通過 lint） |
| `examples/mission_plan.example.json` | plan 檔形狀 |

`SPEC.md` 是這套東西比較不容易複製的一半：它把「為什麼不能相信 worker 說它做完了」
寫成可稽核的條文，而不是一段散文建議。

## 來源

從一套個人 AI agent 知識基礎設施抽出來的，四天內跑過五個真實 mission（約 9 個
background session worker + 13 個 subagent）。文件裡標「實跑」的地方都指向那些，不是
設想的案例——包含幾個只有實跑才會現形的失效，例如上面那個 worker 寫錯地方的例子、
以及模板與它自己的 linter 對不上（照著模板寫會被判不合格）。

## License

MIT
