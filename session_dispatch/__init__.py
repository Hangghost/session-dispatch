"""session-dispatch — 把一輪討論結束後的 N 個下游任務派出去，再收回來。

公開介面刻意窄：規劃與收斂的判斷留給人（或指揮站 agent），本套件只提供
deterministic 的那一半——plan 讀寫、完成訊號三態求值、ready 計算、派工單 lint、
撞名檢查。
"""

from session_dispatch.live_sessions import (
    LiveSession,
    SessionRoster,
    coverage_note,
    enumerate_live_sessions,
    name_is_taken,
    taken_names,
)
from session_dispatch.mission_plan import (
    HOME_ENV_VAR,
    NODE_SHAPES,
    NODE_STATUSES,
    SIGNAL_STATES,
    Mission,
    MissionNode,
    SignalOutcome,
    brief_path,
    evaluate_all,
    evaluate_signal,
    lint_brief,
    main_checkout_root,
    mission_home,
    mission_path,
    read_mission,
    ready_nodes,
    set_node_status,
    unavailable_signals,
    worker_output_dir,
    write_brief,
    write_mission,
)

__all__ = [
    "HOME_ENV_VAR",
    "LiveSession",
    "Mission",
    "MissionNode",
    "NODE_SHAPES",
    "NODE_STATUSES",
    "SIGNAL_STATES",
    "SessionRoster",
    "SignalOutcome",
    "brief_path",
    "coverage_note",
    "enumerate_live_sessions",
    "evaluate_all",
    "evaluate_signal",
    "lint_brief",
    "main_checkout_root",
    "mission_home",
    "mission_path",
    "name_is_taken",
    "read_mission",
    "ready_nodes",
    "set_node_status",
    "taken_names",
    "unavailable_signals",
    "worker_output_dir",
    "write_brief",
    "write_mission",
]

__version__ = "0.1.0"
