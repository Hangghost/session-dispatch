# session-dispatch

**A skill for handing a round of downstream work to independent agent sessions — and getting it back.**

> 🇹🇼 中文版：**[README.zh-TW.md](README.zh-TW.md)**（`SKILL.md` 與 `SPEC.md` 目前也是中文）

Opening several background sessions is one command. This skill is about the four
problems that start *after* that:

- **How do you know a node is actually done?** (not by asking it)
- **What belongs in the brief you hand a worker — and what doesn't?** (coordinates, not knowledge)
- **Can workers talk to each other?** (they can ring the doorbell; they can't pass the task definition)
- **What happens when the work comes back?** (list five things to report, skim only the conclusion, and next time nobody reports carefully)

## Why the completion signal is the core

The very first real run hit this: a worker messaged *"done"*, the plan said pending,
and the file **did exist — inside the worker's own worktree**.

It wasn't lying. It sincerely believed it had finished. The artifact just landed
somewhere the dispatcher couldn't see.

So every node declares a `done_signal`: a shell command whose exit code 0 is the
**only** authority on completion.

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

**A node you can't write that command for should not be dispatched.** If you can't
say "what counts as done" in one command, the task definition hasn't converged yet.
That rule has a side effect worth more than the rule itself: it forces you to decide
the acceptance criteria *before* you hand the work off.

Evaluation returns **three** states, and `unavailable` (the command itself won't run)
is deliberately kept out of `pending`. Render a broken signal as "still running" and
the dispatcher waits forever for a completion that will never arrive.

## The brief: five components

An agent's context has two properties that are the exact inverse of a human's:

- **Zero memory.** The downstream session knows nothing about the half hour you just
  spent thinking. Everything in your head — "I already confirmed it's not A", "I tried
  path B" — does not exist.
- **Retrieval is nearly free.** But it can grep the entire repo in three seconds and
  read five files, cheaply enough to verify any claim on a whim.

For a human colleague it's the opposite: accumulated memory, expensive retrieval. So you
give people conclusions. Giving an agent conclusions is waste — a conclusion is the thing
it can generate cheaply, and yours arrives carrying "might be wrong" risk.

**Spend words only on what the downstream cannot look up.**

| # | Component | Observable evidence |
|---|---|---|
| 1 | Verified premises carry a verification action | Every claim you want believed ships with a near-zero-cost re-check command |
| 2 | Clues and instructions are marked apart | Open-ended sections explicitly say "this is not an instruction" |
| 3 | Judgments written as overridable defaults | Leaning + reasoning + explicit permission to overturn |
| 4 | Boundaries say who holds what | Parallel sessions, branch ownership, files that are off limits |
| 5 | Every report item states its purpose | What you're going to do with the answer |

Component 3 was the most counter-intuitive and the highest-returning. In one mission
with 13 agents, three "I lean this way, but you can overturn me" judgments went into
the briefs — **all three were overturned**. A default with reasoning gives the agent
a target it can cheaply knock down. "Use your own judgment" makes it build the criteria
from scratch, so it reports the status quo instead of arguing with you.

### Mechanical floor

```bash
python3 -c "from session_dispatch import lint_brief; import sys; print(lint_brief(open(sys.argv[1]).read()))" brief.md
```

`lint_brief()` catches **structural omissions** — a whole section missing, a premises
section with no verification command, a report item with no stated purpose. Absent
sections and present-but-incomplete sections produce *different* messages, because your
next action differs (add a section vs. fill one in).

It does **not** catch empty content — `(for reference)` as a stated purpose passes. So
**a green light is not a passing brief**: an aggregate green only verifies *whether*
something is there, never whether it's *right*.

## Nodes can be sessions or subagents

Not every node deserves its own session. `MissionNode.shape` distinguishes the two, and
**shape decides which rules apply**:

| Rule | `session` | `subagent` |
|---|---|---|
| Five-component brief, verify where artifacts landed, synthesis report | ✅ | ✅ |
| `done_signal`, `base_ref`, name-collision check, worktree disposal | ✅ | ❌ |

A subagent has no separate worktree and doesn't report across a channel — the premises
for those rules don't exist, and bolting them on produces ceremony, not guarantees. But
**the brief discipline transfers**, and that has evidence behind it: those three
overturned defaults were all overturned by subagents.

The default is `session`: a missing field gets you the full set of constraints, not
an exemption.

## Install

The skill itself is `SKILL.md`; installation depends on your agent:

```bash
# Claude Code — symlink into ~/.claude/skills/
ln -s "$(pwd)" ~/.claude/skills/session-dispatch

# or via npx skills (cross-agent)
npx skills add "$(pwd)" -g
```

The Python primitives are dependency-free, standard library only:

```bash
pip install -e .          # or just copy session_dispatch/ into your project
pytest                    # 45 tests
```

## Where mission files live

Default is `.claude/missions/` under the main checkout; override with
`SESSION_DISPATCH_HOME`. Path resolution makes **every worktree converge on the same
directory** — otherwise the same mission would read different files depending on which
worktree you happen to be standing in.

These are process artifacts. Add to `.gitignore`:

```gitignore
.claude/missions/
```

## When *not* to use this

The cost is real:

- **Every worker pays a cold start** — reloading your project context and rules. Three
  nodes, three cold starts. If the nodes have sequential dependencies, your parallelism
  is 1: you paid three times and bought nothing. **Doing it yourself in sequence is
  often faster.**
- **N parallel sessions = N times the quota.**
- **Convergence lands back on you.**

But speed isn't the only reason it pays off. Of five real missions, three ran at
parallelism 1 — and none of them were regretted, because what came back wasn't speed.
Two workers reviewing my own freshly written code found a leftover outside the range of
my own grep criteria, and challenged an attribution I'd made about my own module, which
directly improved the API.

So there are two criteria, each standing on its own:

1. **Would doing these separately be faster?** (only true when parallelism > 1)
2. **Do I have a systematic blind spot doing this myself?** (independent perspective —
   unrelated to parallelism)

The first failing doesn't stop the second from holding. **Review, verification, and
finding your own mistakes** live entirely in the second: their value comes from *not
being the same head*, which has nothing to do with running at the same time.

## Documentation

| File | Contents |
|---|---|
| `SKILL.md` | The full workflow: planning, briefs, dispatch, star topology, degradation, convergence, and an anti-rationalization table |
| `SPEC.md` | 15 normative clauses (SHALL / SHALL NOT), each with testable scenarios |
| `references/brief.template.md` | The brief template — canonical, pinned by a test that feeds it verbatim to the linter |
| `examples/mission_plan.example.json` | Plan file shape |

`SPEC.md` is the half that's harder to copy: it turns "why you can't trust a worker's
claim that it's done" into auditable clauses instead of prose advice.

> **Note:** `SKILL.md` and `SPEC.md` are currently written in Traditional Chinese. The
> code, its docstrings' structure, and this README are the English surface. Translation
> of the skill body is on the list — open an issue if you want it sooner.

## Provenance

Extracted from a personal AI-agent knowledge infrastructure, after four days and five
real missions (~9 background session workers + 13 subagents). Everywhere the docs say
"in practice" or "the real run", it points at those — not hypotheticals.

That includes the failures. Two worth naming, because both are the same shape as the
thing this workflow exists to prevent:

- A worker wrote its output somewhere other than where the dispatcher was looking, then
  reported success.
- The brief template didn't pass its own linter, and the linter's most important check
  — "you forgot an entire section" — never fired at all, because it was written as *if
  the section exists, check it*.

**Silence being read as a specific decision.** That's the failure mode, and I shipped it
inside the tool built to catch it.

## License

MIT
