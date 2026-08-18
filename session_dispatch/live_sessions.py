"""live_sessions — 本機 live agent session 的枚舉與撞名檢查。

English summary: enumerates live Claude Code sessions via `claude agents --json`
so the dispatcher can check a planned session name is not already taken. Read-only,
never raises, and degrades explicitly when the CLI is unavailable.

本 module 只做一件事:回答「這個名字有人用了嗎」。它**不做**持有關係比對、不做 worktree
join——那些是 repo 特有的協調需求,不屬於本工作流的核心。

## 兩條設計約束

**降級契約**:`claude` binary 缺席、非零 exit、JSON 無法解析一律回
``SessionRoster(available=False, note=...)``,**SHALL NOT** 回 `available=True` 的空
清單——「查不到」與「真的沒有」不可共用同一個沉默空值。任何 consumer SHALL NOT 因本
原語故障而 block 自身流程。

**覆蓋範圍由資料推導,SHALL NOT 硬編碼**:舊版 `claude agents --json` 只列
background session(前景終端 session 不列);較新版本已改列 interactive 與 background
兩者並為每個條目標上 ``kind``。因此 `covers_interactive` 取「解析到的條目是否帶
``kind``」——該欄位是分得出兩者的版本才輸出,它的存在即是覆蓋 interactive 的證據。

硬編碼的能力聲明會在底層能力變動時**靜默變成不實陳述**。宣稱一個已消失的盲區與隱瞞
一個存在的盲區同屬失真——兩者都讓「已知盲區」清單看起來仍在被維護。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Optional

#: roster 只涵蓋 background 時的尾註（舊版 CLI，條目不帶 `kind`）。
FG_BLIND_SPOT_NOTE = (
    "前景 session 不在偵測範圍（background roster only）；"
    "若有前景終端 session 在該處工作，仍需人工確認。"
)

#: roster 已涵蓋 interactive + background 時的尾註（條目帶 `kind`）。
FULL_COVERAGE_NOTE = "roster 涵蓋 interactive 與 background session（CLI 已標示 kind）。"

#: `claude agents --json` 逾時秒數。roster 是決策的輔助訊號，不值得讓任何命令卡住。
_CLI_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class LiveSession:
    """單一 live session 的可呈現事實。"""

    session_id: str
    name: str
    cwd: str
    status: str = ""  # busy | idle
    state: str = ""  # working | blocked | ...
    pid: Optional[int] = None
    kind: str = ""

    def describe(self) -> str:
        """一行式呈現：``'name'（state，pid 123）於 <cwd>``。"""
        bits = [f"'{self.name or self.session_id}'"]
        detail = ", ".join(
            x
            for x in (self.state or self.status, f"pid {self.pid}" if self.pid else "")
            if x
        )
        if detail:
            bits.append(f"（{detail}）")
        return f"{''.join(bits)} 於 {self.cwd}"


@dataclass(frozen=True)
class SessionRoster:
    """`claude agents --json` 的解析結果。

    `available=False` 表示**查不到**,不表示沒有 session。兩者的差別是本 dataclass
    存在的主要理由。
    """

    available: bool
    sessions: list[LiveSession] = field(default_factory=list)
    note: str = ""
    covers_interactive: bool = False


def _resolve_claude_bin() -> Optional[str]:
    """定位 `claude` binary，不依賴 PATH。

    本模組可能被非 login shell(hook、cron wrapper)呼叫,PATH 常不含 `~/.local/bin`。
    `CLAUDE_BIN` 環境變數優先。
    """
    override = os.environ.get("CLAUDE_BIN", "").strip()
    if override and os.access(override, os.X_OK):
        return override
    found = which("claude")
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ):
        if os.access(cand, os.X_OK):
            return str(cand)
    return None


def enumerate_live_sessions() -> SessionRoster:
    """枚舉本機 live session。純讀、never raise。

    ⚠️ **非 TTY 環境下這件事會失敗,而且是最早發作的失效模式。** `claude agents` 需要
    互動終端機;在 background session 裡執行會非零 exit(`requires an interactive
    terminal`)。本函式因此回 `available=False` 並附原因——這正是為什麼呼叫方不該手刻
    ``claude agents --json | grep -q <name>``:那條管線在同樣情況下只會**安靜地落空**,
    而落空看起來跟「沒撞名」一模一樣。
    """
    binary = _resolve_claude_bin()
    if binary is None:
        return SessionRoster(
            available=False, note="找不到 claude binary（PATH 與已知安裝路徑皆無）"
        )
    try:
        result = subprocess.run(
            [binary, "agents", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return SessionRoster(
            available=False,
            note=f"claude agents --json 逾時（>{_CLI_TIMEOUT_SECONDS}s）",
        )
    except OSError as exc:
        return SessionRoster(
            available=False, note=f"claude agents --json 執行失敗：{exc}"
        )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else "(無 stderr)"
        return SessionRoster(
            available=False,
            note=f"claude agents --json 非零 exit（rc={result.returncode}）：{tail}",
        )

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        return SessionRoster(
            available=False, note=f"claude agents --json 輸出無法解析：{exc}"
        )

    if not isinstance(data, list):
        return SessionRoster(
            available=False,
            note=f"claude agents --json 輸出非預期結構（{type(data).__name__}，預期 list）",
        )

    sessions: list[LiveSession] = []
    saw_kind = False
    for entry in data:
        if not isinstance(entry, dict):
            return SessionRoster(
                available=False, note="claude agents --json 條目非 object，格式已漂移"
            )
        # `kind` 只有分得出 interactive/background 的 CLI 版本才輸出；它的存在即是
        # 覆蓋範圍的證據。用 `in` 而非取值真假——空字串仍代表「這版會標 kind」。
        if "kind" in entry:
            saw_kind = True
        pid = entry.get("pid")
        sessions.append(
            LiveSession(
                session_id=str(entry.get("id") or entry.get("sessionId") or ""),
                name=str(entry.get("name") or ""),
                cwd=str(entry.get("cwd") or ""),
                status=str(entry.get("status") or ""),
                state=str(entry.get("state") or ""),
                pid=pid if isinstance(pid, int) else None,
                kind=str(entry.get("kind") or ""),
            )
        )
    return SessionRoster(available=True, sessions=sessions, covers_interactive=saw_kind)


def taken_names(roster: SessionRoster) -> set[str]:
    """roster 中已被佔用的 session 名稱集合。

    session 名稱即跨 session 訊息的定址鍵,故開新 session 前 SHALL 先驗不撞名——撞名時
    定址需要靠列表附的消歧後綴,而那個後綴呼叫方拿不到穩定值。
    """
    return {s.name for s in roster.sessions if s.name}


def name_is_taken(name: str, roster: Optional[SessionRoster] = None) -> bool:
    """指定名稱是否已被本機某個 session 佔用。

    ``roster`` 省略時現地枚舉一份。**roster 不可用時回 ``False``**(fail-soft:偵測故障
    不該擋住派工)。呼叫方若需要區分「查得到且沒撞」與「查不到」,SHALL 自行檢查
    ``roster.available``,SHALL NOT 依賴本回傳值。
    """
    r = roster if roster is not None else enumerate_live_sessions()
    if not r.available:
        return False
    return name in taken_names(r)


def coverage_note(roster: SessionRoster) -> str:
    """回傳該 roster 該附的覆蓋範圍尾註；呈現層 SHALL 用它，SHALL NOT 自行選文案。

    判定邏輯只住一處。若讓各呼叫點自己判斷 `covers_interactive`,遲早有一處忘了改而
    變成假保證——而那正是本函式取代的那個硬編碼常數的失效形態。
    """
    return FULL_COVERAGE_NOTE if roster.covers_interactive else FG_BLIND_SPOT_NOTE
