# Releasing

發布慣例。**維護者看這份，使用者看 README 的 Versioning 段。**

---

## Tag 格式

`v<MAJOR>.<MINOR>.<PATCH>`，**annotated tag**（`git tag -a`），只打在 `main` 上。

用 annotated 不用 lightweight：annotated tag 是 git object，帶訊息、tagger 與日期。
lightweight tag 只是一個指標，發布記錄會無處可放——而本 repo **刻意不維護 CHANGELOG.md**
（理由見下方「為什麼沒有 CHANGELOG」），tag 訊息就是發布記錄本體。

## 0.x 的版號語意

本專案仍在 `0.x`，語意採 semver 的 0.x 慣例：

| 位置 | 何時進位 |
|---|---|
| MINOR | 新增能力，**或 breaking change**（0.x 允許 minor 帶 breaking） |
| PATCH | 修正、文件、不改變公開介面的重構 |

breaking change SHALL 在 tag 訊息中以 `BREAKING:` 開頭的行載明，並說明舊呼叫該怎麼改。
`0.x` 不留相容層——留了沒人用的相容層，成本會一直付下去。

到 `1.0.0` 之後改為標準 semver（breaking 進 MAJOR）。

## Tag 指向哪個 commit

**該版本在 `main` 上的最終狀態**——正常情況即發布 PR 的 merge commit。

SHALL NOT 打在發布分支的 tip 上再 merge。理由：若該 PR 以 squash 合併，分支 tip 根本不在
`main` 的歷史裡，tag 會指向一個 `main` 上不存在的 commit；即使是 no-ff merge，merge commit
才是「`main` 在發布當下的樣子」。**先 merge，後 tag。**

## 流程

```bash
# 1. 從 main 開發布分支
git checkout -b release/vX.Y.Z main

# 2. 同時 bump 兩處版號（測試會釘住兩者一致，見下）
#    - pyproject.toml     的 version
#    - session_dispatch/__init__.py 的 __version__

# 3. 若測試數／requirement 數有變，更新 README.md 與 README.zh-TW.md 內的數字

# 4. 跑測試
pytest

# 5. 開 PR、merge 進 main

# 6. 切回 main 取得 merge commit，打 tag
git checkout main && git pull --ff-only
git tag -a vX.Y.Z -m "vX.Y.Z — <一句話主題>

<條列本次變更>

BREAKING: <若有，舊呼叫 → 新呼叫>"

# 7. 推 tag（與推分支是兩件事，tag 要單獨推）
git push origin vX.Y.Z
```

## 為什麼沒有 CHANGELOG.md

CHANGELOG 是**第三份**要手維護的發布記錄（tag 訊息、GitHub Release、CHANGELOG），而三份
手維護的同一事實會各自漂移——這正是本專案在 `SPEC.md`「推翻既有條文的實測校正 SHALL 回填
被推翻的位置」那條要防的形狀。

發布記錄的單一來源是 **annotated tag 的訊息**：

```bash
git tag -n99                 # 列出所有版本與完整訊息
git show v0.2.0              # 單一版本的完整訊息與 diff 起點
git log v0.1.0..v0.2.0       # 兩版之間的 commit
```

要 GitHub Release 頁面時，從 tag 生成即可（`gh release create vX.Y.Z --notes-from-tag`），
內容仍源自 tag，不產生第二份真相。

## 機械化的部分

慣例中**可被驗證的那一半**由測試強制，不靠自律：

| 檢查 | 位置 | 擋住什麼 |
|---|---|---|
| `pyproject.toml` 的 version ＝ `__init__.__version__` | `tests/test_release.py` | 兩份手維護的版號靜默漂移——bump 了一處忘了另一處，而安裝者拿到的版號取決於他用哪個管道 |
| 版號形狀為 `MAJOR.MINOR.PATCH` | 同上 | 打錯成 `0.2` 或 `v0.2.0` |

**不可被驗證的那一半**（tag 有沒有真的打、有沒有打在 merge commit 上）留在本文件。
離線測試查不到 remote 的 tag 狀態，硬做會得到一個依賴網路且會在無網路時紅燈的測試——
那比沒有測試更糟。這是刻意的邊界，不是遺漏。

## 歷史

`v0.1.0` 是**回填的 tag**：該版發布當時尚未建立本慣例，tag 於 `v0.2.0` 之後補上，指向
`main` 上 0.1.0 的最終狀態（`dc3825b`）。tagger date 已對齊該 commit 的日期，故
`git tag --sort=creatordate` 的順序仍然正確。
