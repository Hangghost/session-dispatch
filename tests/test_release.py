"""發布慣例中**可被驗證的那一半**。

慣例全文見 `RELEASING.md`。這裡只釘住兩件離線查得到的事：版號的兩份手維護副本一致，
以及版號的形狀合法。

## 為什麼這是必要的

`pyproject.toml` 的 `version` 與 `session_dispatch.__version__` 是**同一個事實的兩份
手抄本**。bump 時漏改一處不會有任何訊號——套件照樣裝得起來、測試照樣全綠，而安裝者
拿到的版號取決於他從哪個管道讀：`pip show` 讀前者，`session_dispatch.__version__` 讀
後者。兩者不一致時，「我裝的是哪一版」這個問題沒有答案。

這與本專案 `SKILL.md` 的派工單模板曾與其 lint 不一致是同一形態：**規格的兩個面必須
互相釘住，否則它們會各自演化**。那次的教訓是加回歸測試，這裡照做。

## 為什麼不驗「tag 是否存在」

離線查不到 remote 的 tag 狀態。硬做會得到一個依賴網路、且在無網路時紅燈的測試——
那比沒有測試更糟：一個會因為與被測行為無關的原因而失敗的測試，會訓練讀者忽略它。
tag 有沒有真的打、有沒有打在 merge commit 上，留在 `RELEASING.md` 的散文層。
"""

from __future__ import annotations

import re
from pathlib import Path

import session_dispatch

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: `MAJOR.MINOR.PATCH`，三段皆為數字。刻意不含 `v` 前綴——`v` 只出現在 git tag 上，
#: 套件內的版號字串不帶它（`pip` 與 PEP 440 都不接受）。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _pyproject_version() -> str:
    """從 `pyproject.toml` 取 `[project]` 的 version。

    以正則而非 `tomllib` 解析：`requires-python` 為 `>=3.9`，而 `tomllib` 是 3.11+
    才進標準庫。為了一個單行取值引入 `tomli` 依賴，會讓一個刻意零依賴的套件多一個
    依賴——那個代價比這幾行正則貴。
    """
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            _, _, raw = stripped.partition("=")
            return raw.strip().strip('"').strip("'")
    raise AssertionError(f"{PYPROJECT} 內找不到 version 行")


def test_package_version_matches_pyproject():
    """兩份手維護的版號 SHALL 一致——漏改一處在此紅燈，而不是在安裝者手上。"""
    assert session_dispatch.__version__ == _pyproject_version(), (
        f"版號漂移：session_dispatch.__version__ = {session_dispatch.__version__!r}，"
        f"而 pyproject.toml = {_pyproject_version()!r}。"
        "bump 版本時兩處都要改（見 RELEASING.md）。"
    )


def test_version_shape_is_major_minor_patch():
    """版號 SHALL 為 `MAJOR.MINOR.PATCH`，不帶 `v` 前綴、不省略段。"""
    version = session_dispatch.__version__
    assert _VERSION_RE.match(version), (
        f"版號形狀不合法：{version!r}。SHALL 為 MAJOR.MINOR.PATCH（三段皆數字、"
        "無 `v` 前綴）——`v` 只出現在 git tag 上。"
    )


def test_releasing_doc_exists_and_names_both_version_sites():
    """`RELEASING.md` SHALL 指名兩處版號的位置。

    測試釘住的是一致性，**沒有釘住「該去哪裡改」**——一個只說「兩處不一致」而不說
    是哪兩處的紅燈，會讓讀者自己去找。那份文件是這個測試的錯誤訊息的延伸，所以它
    的存在與內容也是被測項。
    """
    doc = REPO_ROOT / "RELEASING.md"
    assert doc.exists(), "RELEASING.md 不存在——發布慣例無處可查"

    text = doc.read_text(encoding="utf-8")
    assert "pyproject.toml" in text, "RELEASING.md 未指名 pyproject.toml"
    assert "__version__" in text, "RELEASING.md 未指名 __version__"
