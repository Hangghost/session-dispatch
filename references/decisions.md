# 決策記錄

這份文件是 session-dispatch 成形時被正面否決的替代方案與其理由——SKILL.md §8 只放指針，
這裡是「有人問起才需要展開」的那一層。

### 為什麼不用官方 agent teams

沿用先前一次前置探勘與路由設計的判斷並補一項：

| 理由 | 說明 |
|---|---|
| **無 worktree 隔離** | 最關鍵一項。本工作流的隔離正是靠 `--worktree` 讓每個 worker 有自己的 checkout；agent teams 靠「任務切檔案」，多節點動到相鄰檔案就會互踩 |
| 實驗性 | 需 env flag 開啟，`resume` 不還原 teammates |
| token 成本 | 每個 teammate 是完整 instance，且 lead 全程持有 |
| 與並行紀律精神衝突 | 另一份內部規約對並行 subagent 數量設有上限，理由同構 |

### 與另一種 project 級任務追蹤機制的邊界

機制高度重疊（節點、依賴、執行綁定），邊界畫在**壽命**：那個機制是 project 級、跨月、進
git；mission 是一輪對話級、machine-local、做完即棄。因此本工作流 SHALL NOT 引入
provenance、確定性 id、衝突收斂或跨機同步；要加請先正面推翻 spec 條文，SHALL NOT 漸進繞過。
完整對照見 `session_dispatch/mission_plan.py` 的 module docstring。

---
