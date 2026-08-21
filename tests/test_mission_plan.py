"""`session_dispatch.mission_plan` 的行為測試。

重點不在覆蓋率，在**五個容易靜默壞掉的地方**：

1. 求值失敗必須是顯式第三態，不得併入「未完成」
2. 訊號優先於 plan 的 status——worker 宣稱完成但訊號沒出現時不得推進下游
3. `session` 形態的必填欄位缺席必須在建構期就炸，不是派工後才發現
4. 派工單模板必須逐字通過它自己的 lint（兩者是同一份規格的兩個面）
5. lint 的元件缺席必須紅燈——放過「整段忘了寫」是最糟的失效方向
"""

from __future__ import annotations

from pathlib import Path

import pytest

from session_dispatch import mission_plan as mp

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "references" / "brief.template.md"
SKILL_PATH = REPO_ROOT / "SKILL.md"


def _node(nid: str = "a", **kw) -> mp.MissionNode:
    base = dict(
        id=nid,
        session_name=f"m1-{nid}",
        brief_path=f"/tmp/{nid}.brief.md",
        done_signal="true",
        base_ref="main",
    )
    base.update(kw)
    return mp.MissionNode(**base)


def _mission(*nodes: mp.MissionNode) -> mp.Mission:
    return mp.Mission(
        mission_id="m1", created_at="2026-08-15T10:00:00", nodes=tuple(nodes)
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """每個測試各自的 mission home，避免碰到跑測試那台機器的真實檔案。"""
    monkeypatch.setenv(mp.HOME_ENV_VAR, str(tmp_path / "missions"))


# --------------------------------------------------------------------------- 建構期驗證


def test_missing_done_signal_raises_at_construction():
    """無法宣告完成訊號的節點不該進到派工階段。"""
    with pytest.raises(ValueError, match="done_signal"):
        _node(done_signal="   ")


def test_missing_base_ref_raises_at_construction():
    """base 必須顯式選擇——worker worktree 會隱式繼承指揮站 HEAD。"""
    with pytest.raises(ValueError, match="base_ref"):
        _node(base_ref="")


def test_base_ref_omission_explodes_at_construction():
    """漏填 base_ref 在建構期就炸——決定藏不回去。

    斷言的是**保證**（漏填會炸、訊息指名欄位）而非機制。欄位帶空字串預設，攔截點在
    `__post_init__` 而非 dataclass 的 missing-argument。
    """
    with pytest.raises(ValueError, match="缺 base_ref"):
        mp.MissionNode(
            id="a", session_name="x", brief_path="/tmp/x", done_signal="true"
        )


def test_unknown_status_rejected():
    with pytest.raises(ValueError, match="未知的節點狀態"):
        _node(status="halfway")


# --------------------------------------------------------------------------- 節點形態


def test_subagent_node_needs_no_done_signal_or_base_ref():
    """subagent 沒有 worktree、也不隔著通道回報，那兩條的前提不存在。"""
    node = mp.MissionNode(
        id="s", session_name="m1-s", brief_path="/tmp/s.brief.md", shape="subagent"
    )

    assert node.shape == "subagent"
    assert node.done_signal == ""


def test_subagent_node_still_needs_a_brief():
    """派工單落檔是跨形態義務。"""
    with pytest.raises(ValueError, match="brief_path"):
        mp.MissionNode(
            id="s", session_name="m1-s", brief_path="  ", shape="subagent"
        )


def test_unknown_shape_rejected():
    with pytest.raises(ValueError, match="未知的節點形態"):
        _node(shape="teammate")


def test_shape_defaults_to_session():
    """漏填 SHALL 落在約束較嚴格的一側——得到完整約束，不是豁免。"""
    assert _node().shape == "session"


def test_empty_done_signal_is_never_evaluated_as_done():
    """空指令在 shell 下回 0——照常求值會把「沒宣告訊號」判成「已完成」。"""
    node = mp.MissionNode(
        id="s", session_name="m1-s", brief_path="/tmp/s.brief.md", shape="subagent"
    )
    outcome = mp.evaluate_signal(node)

    assert outcome.state == "pending"
    assert "done_signal" in outcome.detail


def test_subagent_nodes_are_not_signal_evaluated(monkeypatch):
    """subagent 不求值，且 SHALL NOT 被列為 unavailable（那會渲染成壞掉的訊號）。"""
    calls: list[str] = []
    monkeypatch.setattr(
        mp,
        "evaluate_signal",
        lambda n, cwd=None: calls.append(n.id)
        or mp.SignalOutcome(n.id, "pending"),
    )
    m = _mission(
        mp.MissionNode(
            id="s", session_name="m1-s", brief_path="/tmp/s.brief.md", shape="subagent"
        ),
        _node("t"),
    )
    out = mp.evaluate_all(m)

    assert calls == ["t"]
    assert out["s"].state == "pending"
    assert out["s"].state != "unavailable"


# --------------------------------------------------------------------------- 路徑


def test_mission_home_respects_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(mp.HOME_ENV_VAR, str(tmp_path / "custom"))
    assert mp.mission_home() == tmp_path / "custom"


def test_mission_home_defaults_under_claude_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(mp.HOME_ENV_VAR, raising=False)
    monkeypatch.setattr(mp, "main_checkout_root", lambda repo=None: tmp_path)

    assert mp.mission_home() == tmp_path / ".claude" / "missions"


def test_paths_are_derived_from_one_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv(mp.HOME_ENV_VAR, str(tmp_path / "m"))
    p = mp.mission_path("m1")
    b = mp.brief_path("m1", "a")

    assert p == tmp_path / "m" / "m1.json"
    assert b == tmp_path / "m" / "m1" / "a.brief.md"


def test_worker_output_dir_points_at_worker_worktree(tmp_path, monkeypatch):
    """產物落 worker 自己的容器——mission 目錄 worker 未必寫得進去。"""
    monkeypatch.setattr(mp, "main_checkout_root", lambda repo=None: tmp_path)
    p = mp.worker_output_dir(_node("a", session_name="selfcheck-a"))

    assert p == tmp_path / ".claude" / "worktrees" / "selfcheck-a"
    assert "missions" not in str(p)


def test_worker_output_dir_refuses_to_guess_cross_repo_container(tmp_path):
    """跨 repo 節點的容器名指揮站推不準——SHALL 在呼叫點炸，不得回一條猜的路徑。

    回猜測值的失效形態是「永遠 pending」，與「worker 還在做」同形；當場炸至少看得見。
    """
    node = _node("a", workspace_repo=str(tmp_path))
    with pytest.raises(ValueError, match="container_name"):
        mp.worker_output_dir(node)


def test_worker_output_dir_accepts_explicit_cross_repo_container(tmp_path, monkeypatch):
    """容器名由呼叫方顯式給出時，跨 repo 節點照常推導。"""
    monkeypatch.setattr(mp, "main_checkout_root", lambda repo=None: tmp_path)
    node = _node("a", workspace_repo=str(tmp_path))

    assert mp.worker_output_dir(node, "xrepo-smoke") == (
        tmp_path / ".claude" / "worktrees" / "xrepo-smoke"
    )


def test_main_checkout_root_never_raises_outside_git(tmp_path):
    """非 git 目錄 SHALL 退回 start，不得 raise。"""
    assert mp.main_checkout_root(tmp_path) == tmp_path


# --------------------------------------------------------------------------- 讀寫


def test_write_then_read_roundtrip():
    m = _mission(_node("a"), _node("b", depends_on=("a",), needs_exclusive_checkout=True))
    mp.write_mission(m)
    got = mp.read_mission("m1")

    assert got is not None
    assert [n.id for n in got.nodes] == ["a", "b"]
    assert got.node("b").depends_on == ("a",)
    assert got.node("b").needs_exclusive_checkout is True


def test_legacy_plan_without_shape_reads_as_session(tmp_path):
    """既有 plan 檔不帶 shape 仍能讀入，且落在較嚴格的一側。"""
    path = mp.mission_path("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"mission_id":"legacy","created_at":"","nodes":[{"id":"a",'
        '"session_name":"x","brief_path":"/tmp/a.md","done_signal":"true",'
        '"base_ref":"main"}]}',
        encoding="utf-8",
    )
    got = mp.read_mission("legacy")

    assert got is not None
    assert got.node("a").shape == "session"


def test_read_missing_mission_returns_none():
    assert mp.read_mission("nope") is None


def test_read_corrupt_mission_returns_none():
    path = mp.mission_path("m1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert mp.read_mission("m1") is None


def test_write_brief_creates_parent_and_returns_path():
    p = mp.write_brief("m1", "a", "# 派工單\n內容\n")

    assert p.read_text(encoding="utf-8").startswith("# 派工單")
    assert p == mp.brief_path("m1", "a")


def test_set_node_status_persists():
    m = _mission(_node("a"), _node("b"))
    mp.write_mission(m)
    updated = mp.set_node_status(m, "a", "done")

    assert updated.node("a").status == "done"
    assert mp.read_mission("m1").node("a").status == "done"
    assert m.node("a").status == "pending"  # frozen：原物件不被就地改


def test_set_node_status_unknown_node_raises():
    m = _mission(_node("a"))
    mp.write_mission(m)
    with pytest.raises(KeyError):
        mp.set_node_status(m, "zzz", "done")


# --------------------------------------------------------------------------- 訊號求值三態


def test_signal_exit_zero_is_done():
    assert mp.evaluate_signal(_node(done_signal="true")).state == "done"


def test_signal_nonzero_is_pending():
    out = mp.evaluate_signal(_node(done_signal="false"))
    assert out.state == "pending"
    assert out.is_done is False


def test_signal_timeout_is_unavailable_not_pending(monkeypatch):
    """求值失敗 SHALL NOT 併入「未完成」——否則壞掉的訊號看起來像進行中的節點。"""
    import subprocess

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=30)

    monkeypatch.setattr(mp.subprocess, "run", boom)
    out = mp.evaluate_signal(_node())

    assert out.state == "unavailable"
    assert out.state != "pending"
    assert "逾時" in out.detail


def test_signal_oserror_is_unavailable(monkeypatch):
    def boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(mp.subprocess, "run", boom)
    out = mp.evaluate_signal(_node())

    assert out.state == "unavailable"
    assert "無法執行" in out.detail


def test_unavailable_signals_are_separately_listable():
    outcomes = {
        "a": mp.SignalOutcome("a", "done"),
        "b": mp.SignalOutcome("b", "pending"),
        "c": mp.SignalOutcome("c", "unavailable", "boom"),
    }
    assert [o.node_id for o in mp.unavailable_signals(outcomes)] == ["c"]


# --------------------------------------------------------------------------- ready 計算


def test_signal_outranks_plan_status():
    """worker 宣稱完成（status=done）但訊號未出現時，下游 SHALL NOT 被放行。"""
    m = _mission(_node("a", status="pending"), _node("b", depends_on=("a",)))
    outcomes = {"a": mp.SignalOutcome("a", "pending"), "b": mp.SignalOutcome("b", "pending")}

    assert [n.id for n in mp.ready_nodes(m, outcomes)] == ["a"]


def test_dependency_satisfied_by_signal_releases_downstream():
    m = _mission(_node("a"), _node("b", depends_on=("a",)))
    outcomes = {"a": mp.SignalOutcome("a", "done"), "b": mp.SignalOutcome("b", "pending")}

    assert [n.id for n in mp.ready_nodes(m, outcomes)] == ["b"]


def test_only_one_exclusive_checkout_node_at_a_time():
    """兩個都要獨佔主 checkout 的節點同時派出等於製造互等。"""
    m = _mission(
        _node("a", needs_exclusive_checkout=True),
        _node("b", needs_exclusive_checkout=True),
        _node("c"),
    )

    assert [n.id for n in mp.ready_nodes(m)] == ["a", "c"]


def test_dispatched_nodes_are_not_re_offered():
    m = _mission(_node("a", status="dispatched"), _node("b"))
    assert [n.id for n in mp.ready_nodes(m)] == ["b"]


# --------------------------------------------------------------------------- 派工單 lint

_GOOD_BRIEF = """# 派工單：x

## 任務
做一件事。

## 已查證的前提
1. 那個函式會 fallback 到 EOF
   驗：`grep -n "Unread" path`

## 線索（不是指令）
- 修法有幾種？

## 邊界
- 不要動 main。

## 完成訊號
`test -f /tmp/x.md`

## 回報（每項附用途）
1. 採用的修法（我要拿去比對 applier 契約）
2. 被否決的方案（我要拿去判斷要不要開後續票）
"""


def test_good_brief_passes_lint():
    assert mp.lint_brief(_GOOD_BRIEF) == []


def test_report_item_without_purpose_is_rejected():
    bad = _GOOD_BRIEF.replace(
        "1. 採用的修法（我要拿去比對 applier 契約）", "1. 採用的修法"
    )
    findings = mp.lint_brief(bad)

    assert any("未附用途" in f for f in findings)


def test_premise_without_verification_command_is_rejected():
    bad = _GOOD_BRIEF.replace('   驗：`grep -n "Unread" path`\n', "")
    assert any("驗證動作" in f for f in mp.lint_brief(bad))


def test_clue_section_without_not_an_instruction_marker_is_rejected():
    bad = _GOOD_BRIEF.replace("## 線索（不是指令）", "## 線索")
    assert any("不是指令" in f for f in mp.lint_brief(bad))


def test_missing_boundary_and_signal_sections_are_rejected():
    bad = _GOOD_BRIEF.replace("## 邊界\n- 不要動 main。\n\n", "").replace(
        "## 完成訊號\n`test -f /tmp/x.md`\n\n", ""
    )
    findings = mp.lint_brief(bad)

    assert any("邊界" in f for f in findings)
    assert any("完成訊號" in f for f in findings)


def test_multiline_report_item_with_wrapped_purpose_passes():
    """用途說明跨行時不得誤判為缺用途——實測真實派工單各中 2 次。"""
    wrapped = _GOOD_BRIEF.replace(
        "1. 採用的修法（我要拿去比對 applier 契約）",
        "1. 採用的修法（我要拿去比對另一支變更的\n   applier sink 契約）",
    )
    assert mp.lint_brief(wrapped) == []


# --------------------------------------------------------------------------- 模板與 lint 的契約


def _inline_template_from_skill() -> str:
    """抽出 SKILL.md 內嵌的模板副本並去掉 4 空格縮排。"""
    text = SKILL_PATH.read_text(encoding="utf-8")
    body = text.split("<!-- BRIEF_TEMPLATE_BEGIN -->")[1].split(
        "<!-- BRIEF_TEMPLATE_END -->"
    )[0]
    lines = [ln[4:] if ln.startswith("    ") else ln for ln in body.splitlines()]
    return "\n".join(lines).strip() + "\n"


def test_canonical_template_passes_lint_verbatim():
    """模板與檢查器是同一份規格的兩個面，不一致時使用者無從判斷該信哪個。

    本測試釘住的是**模板檔本身**，不是測試裡另寫一份合格範例——後者驗不到這個縫。
    """
    assert mp.lint_brief(TEMPLATE_PATH.read_text(encoding="utf-8")) == []


def test_skill_inline_template_matches_canonical_file():
    """SKILL.md 的內嵌副本與 canonical 檔逐字相同——改一邊不改另一邊會紅燈。"""
    assert _inline_template_from_skill() == TEMPLATE_PATH.read_text(encoding="utf-8")


def test_dash_marker_sections_are_recognised():
    """lint 對格式寬容、對內容嚴格——等價標記不該產生假陽性。"""
    dashed = _GOOD_BRIEF
    for title in (
        "任務",
        "已查證的前提",
        "線索（不是指令）",
        "邊界",
        "完成訊號",
        "回報（每項附用途）",
    ):
        dashed = dashed.replace(f"## {title}", f"—— {title} ——")

    assert mp.lint_brief(dashed) == []


def test_prose_dash_does_not_create_phantom_section():
    """句中的破折號不是段落標記——只認整行前後皆有標記的形狀。"""
    noisy = _GOOD_BRIEF.replace("做一件事。", "做一件事——這句話裡有破折號，但它不是標記。")

    assert mp.lint_brief(noisy) == []


def test_missing_premise_section_is_reported_not_silently_passed():
    """整段忘了寫是最該被擋的形態，早期版本恰好放過它。"""
    bad = _GOOD_BRIEF.replace(
        '## 已查證的前提\n1. 那個函式會 fallback 到 EOF\n   驗：`grep -n "Unread" path`\n\n',
        "",
    )
    findings = mp.lint_brief(bad)

    assert any("元件 1" in f and "缺" in f for f in findings)


def test_missing_clue_section_is_reported_not_silently_passed():
    bad = _GOOD_BRIEF.replace("## 線索（不是指令）\n- 修法有幾種？\n\n", "")
    findings = mp.lint_brief(bad)

    assert any("元件 2" in f and "缺" in f for f in findings)


def test_absent_and_incomplete_sections_give_distinguishable_findings():
    """缺席與內容缺項的下一步動作不同，訊息 SHALL 可區分。"""
    absent = mp.lint_brief(_GOOD_BRIEF.replace("## 線索（不是指令）\n- 修法有幾種？\n\n", ""))
    incomplete = mp.lint_brief(_GOOD_BRIEF.replace("## 線索（不是指令）", "## 線索"))

    absent_msg = next(f for f in absent if "元件 2" in f)
    incomplete_msg = next(f for f in incomplete if "元件 2" in f)
    assert absent_msg != incomplete_msg


def test_example_plan_actually_parses(monkeypatch, tmp_path):
    """範例檔 SHALL 能被 `read_mission()` 讀入。

    一份讀不進去的範例比沒有範例更糟——它會被複製貼上，然後在別人的機器上安靜地回
    `None`。首版的節點物件帶了 `_comment_*` 說明鍵，`MissionNode(**n)` 因未知關鍵字
    參數而 raise，整份 plan 被拒收；說明因此改放頂層。
    """
    import json
    import shutil

    src = REPO_ROOT / "examples" / "mission_plan.example.json"
    raw = json.loads(src.read_text(encoding="utf-8"))
    home = tmp_path / "missions"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(mp.HOME_ENV_VAR, str(home))
    shutil.copyfile(src, home / f"{raw['mission_id']}.json")

    got = mp.read_mission(raw["mission_id"])

    assert got is not None, "範例 plan 無法被解析"
    assert [n.id for n in got.nodes] == ["audit", "fix", "survey", "land", "xrepo"]
    assert got.node("survey").shape == "subagent"
    assert got.node("land").needs_exclusive_checkout is True
    assert got.node("xrepo").workspace_repo.startswith("/")
    assert got.node("xrepo").work_branch


# --------------------------------------------------------------------------- 跨 repo 節點


def test_relative_workspace_repo_raises_at_construction():
    """相對路徑的基準是 process cwd，而 cwd 會在 session 中途改變——建構期就擋。"""
    with pytest.raises(ValueError, match="絕對路徑"):
        _node("a", workspace_repo="../other-repo")


def test_missing_workspace_repo_is_unavailable_not_pending(tmp_path):
    """錨錯 repo 的訊號回非零，會被誤判成「還在做」——SHALL 升級為 unavailable。

    這是本工作流最貴的一種靜默失敗：一個永遠不會 fire 的訊號，長得跟一個仍在工作的
    worker 一模一樣，於是指揮站永遠等下去。
    """
    node = _node("a", workspace_repo=str(tmp_path / "gone"), done_signal="false")
    out = mp.evaluate_signal(node)

    assert out.state == "unavailable"
    assert "workspace_repo" in out.detail


def test_non_git_workspace_repo_is_unavailable(tmp_path):
    """路徑存在但不是 git repository，同樣是能力故障不是進度狀態。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    node = _node("a", workspace_repo=str(plain), done_signal="false")

    assert mp.evaluate_signal(node).state == "unavailable"


def test_unrunnable_exit_code_is_unavailable(tmp_path):
    """127（找不到指令）語意無歧義——SHALL NOT 併入「還沒做完」。"""
    out = mp.evaluate_signal(_node("a", done_signal="no_such_command_xyz"))

    assert out.state == "unavailable"


def test_ordinary_nonzero_exit_stays_pending():
    """退出碼 1 與「檢查為否」同形，SHALL NOT 被升級——那是第三態的反向誤用。"""
    assert mp.evaluate_signal(_node("a", done_signal="false")).state == "pending"


def test_exclusive_checkout_gate_is_per_workspace_repo(tmp_path):
    """不同 repo 的主 checkout 是獨立資源，其鎖不互相排斥。"""
    here = _node("a", needs_exclusive_checkout=True)
    there = _node(
        "b", needs_exclusive_checkout=True, workspace_repo=str(tmp_path)
    )
    ready = mp.ready_nodes(_mission(here, there))

    assert {n.id for n in ready} == {"a", "b"}


def test_exclusive_checkout_gate_still_serialises_same_repo():
    """同一個 repo 內仍至多一個——同時派出兩個是製造互等。"""
    a = _node("a", needs_exclusive_checkout=True)
    b = _node("b", needs_exclusive_checkout=True)

    assert len(mp.ready_nodes(_mission(a, b))) == 1


# --------------------------------------------------------------------------- dispatch


def test_dispatch_plan_keeps_brief_at_station_for_cross_repo(tmp_path, monkeypatch):
    """派工單恆在指揮站，worker 靠附加目錄授權讀——SHALL NOT 複製進目標 repo。"""
    monkeypatch.setenv(mp.HOME_ENV_VAR, str(tmp_path / "missions"))
    target = tmp_path / "other"
    target.mkdir()
    plan = mp.dispatch_plan(_node("a", workspace_repo=str(target)), "m1")

    assert plan.cwd == target
    assert plan.brief_abs == tmp_path / "missions" / "m1" / "a.brief.md"
    assert plan.add_dirs == (tmp_path / "missions" / "m1",)
    assert not _is_relative(plan.brief_abs, target)


def test_dispatch_plan_cross_repo_defaults_to_worker_owned_container(tmp_path):
    """未指定容器名的跨 repo 節點：容器由 worker 自建，指揮站 SHALL NOT 推導路徑。"""
    target = tmp_path / "other"
    target.mkdir()
    plan = mp.dispatch_plan(_node("a", workspace_repo=str(target)), "m1")

    assert plan.container_from_worker is True
    assert plan.container_name == ""


def test_dispatch_plan_same_repo_needs_no_extra_grant(tmp_path, monkeypatch):
    """同 repo 節點不需附加目錄授權——那個授權會擴大 worker 的可及範圍。"""
    monkeypatch.setattr(mp, "main_checkout_root", lambda repo=None: tmp_path)
    plan = mp.dispatch_plan(_node("a", session_name="m1-a"), "m1")

    assert plan.add_dirs == ()
    assert plan.container_name == "m1-a"
    assert plan.container_from_worker is False


def _is_relative(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------- 收尾


def test_teardown_without_work_branch_is_unavailable_not_disposed():
    """缺錨點時枚舉全落空，外觀與「已處置」相同——回 disposed 就是用「沒找到」冒充「不存在」。"""
    plan = mp.plan_worker_teardown(_node("a", work_branch=""))

    assert plan.state == "unavailable"
    assert plan.is_disposed is False
    assert "work_branch" in (plan.reason or "")


def test_teardown_unreachable_repo_is_unavailable(tmp_path):
    """目標 repo 不可達時同理——不可達不等於已收乾淨。"""
    node = _node("a", workspace_repo=str(tmp_path / "gone"), work_branch="wt-a")

    assert mp.plan_worker_teardown(node).state == "unavailable"


def test_teardown_disposed_when_branch_holds_no_worktree(tmp_path):
    """有可信錨點且枚舉無一命中，才是一句能成立的正面斷言。"""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    node = _node("a", workspace_repo=str(repo), work_branch="never-existed")

    plan = mp.plan_worker_teardown(node)
    assert plan.state == "disposed"
    assert plan.worktree_path is None


def test_teardown_steps_unlock_before_remove():
    """實測順序約束：session 停掉後 lock 仍在，少了 unlock 第一個指令就失敗。"""
    steps = mp._teardown_steps(
        repo="/r",
        worktree_path="/r/.claude/worktrees/w",
        session_name="m1-a",
        blocked=False,
        is_main_checkout=False,
    )
    unlock = next(i for i, s in enumerate(steps) if "worktree unlock" in s)
    remove = next(i for i, s in enumerate(steps) if "worktree remove" in s)

    assert unlock < remove


def test_teardown_steps_never_delete_branches():
    """分支去留是目標 repo 的政策，指揮站據自己的慣例判定外來分支只會錯。"""
    steps = mp._teardown_steps(
        repo="/r",
        worktree_path="/r/.claude/worktrees/w",
        session_name="m1-a",
        blocked=False,
        is_main_checkout=False,
    )

    assert not any("branch -D" in s for s in steps)


def test_teardown_steps_skip_removal_while_container_is_held():
    """仍被佔用的容器不該被建議拆除，但 session 處置照列。"""
    steps = mp._teardown_steps(
        repo="/r",
        worktree_path="/r/.claude/worktrees/w",
        session_name="m1-a",
        blocked=True,
        is_main_checkout=False,
    )

    assert not any("worktree remove" in s for s in steps)
    assert any("claude rm" in s for s in steps)


def test_teardown_steps_carry_explicit_repo_anchor():
    """每項都帶 `git -C`：由人執行時，未定位的 git 指令會作用在錯誤的 repo 上。"""
    steps = mp._teardown_steps(
        repo="/r",
        worktree_path="/r/.claude/worktrees/w",
        session_name="",
        blocked=False,
        is_main_checkout=False,
    )

    assert steps and all(s.startswith("git -C /r ") for s in steps)


# --------------------------------------------------------------------------- 隨附範例


def test_shipped_example_covers_every_node_shape():
    """範例 SHALL 涵蓋四種形態，否則新增的欄位沒有任何示範，使用者只能讀 docstring。"""
    import json

    raw = json.loads((REPO_ROOT / "examples" / "mission_plan.example.json").read_text())
    nodes = raw["nodes"]

    assert any(n["shape"] == "subagent" for n in nodes), "缺 subagent 節點示範"
    assert any(n["needs_exclusive_checkout"] for n in nodes), "缺獨佔主 checkout 節點示範"
    assert any(n.get("workspace_repo") for n in nodes), "缺跨 repo 節點示範"
    xrepo = next(n for n in nodes if n.get("workspace_repo"))
    assert xrepo.get("work_branch"), "跨 repo 節點缺 work_branch——收尾時無定位錨點"
    assert "git log" in xrepo["done_signal"], "跨 repo 節點的訊號 SHALL 為 commit 形態"
