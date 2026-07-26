# Ralph Loop v3 🤖

A deterministic Codex orchestrator for implementing complete modules from a
PRD. The model performs the creative work, while the orchestrator controls state
transitions, file scope, validation, and finalization.

## Workflow

```text
📋 task contract
   ↓
🧠 Planner ──→ JSON result and file-scope validation
   ↓
🛠️  Implementer ──→ 🧪 deterministic quality gates
   ↑                         ↓
   └──── fixes ←──── 🔍 independent Reviewer
                              ↓ PASS
                   🧪 final quality gates
                              ↓
                   📦 commit → 🔗 fast-forward
```

Every task runs on an isolated branch in a separate Git worktree. The primary
branch moves forward only after `PASS`, another successful quality-gate run, and
a confirmed commit. Role results, complete logs, and the append-only journal are
stored under `.ralph/runtime/` and are never committed.

## Getting started

Requirements: Python 3.9+, Git, and an authenticated Codex CLI.

```bash
./ralph init --prd docs/my-module-prd.md \
  --manifest docs/tasks/my-module/manifest.json

# Complete the PRD, manifest, and task.md, then commit the initial state.
./ralph doctor
./ralph status
./ralph run
```

Installing a system-wide command is optional:

```bash
python3 -m pip install -e .
ralph run
```

## Task contract

A complete example is available in
[`examples/manifest.json`](examples/manifest.json), with a task-description
template in [`examples/task.md`](examples/task.md).

Every task must define:

- a stable identifier and dependencies;
- explicit `AC-*` acceptance criteria;
- `contract.allowedPaths`, which technically enforces the implementer's scope;
- quality gates represented as argument arrays such as `["npm", "test"]`, with
  no `sh -c` and no scripts extracted from Markdown;
- separate attempt limits for planning, implementation, and review;
- a task directory containing `task.md`.

Statuses form an explicit state machine:

```text
ready → planning → planned → implementing → in_review → completed
            ↑              ↖ needs_changes ↙       │
            └──────── needs_replan ─────────────────┘
```

The `blocked` and `failed` states stop the session without merging partial code.
The isolated branch and worktree remain available for inspection, and their
locations are shown in the terminal.

## Determinism and safety

- Every role result is checked against a JSON Schema and then validated for
  complete evidence covering every `AC-*` criterion.
- The planner may modify only `plan.md`, the reviewer only `review.md`, and the
  implementer only `progress.md` plus paths allowed by the task contract.
- Any out-of-scope modification stops the workflow immediately.
- Quality gates run without a shell, with a timeout and fixed `TZ`, locale, and
  `PYTHONHASHSEED` values.
- Agent network access is disabled by default. It can be enabled explicitly in
  `.ralph/config.json` when a task genuinely requires it.
- The same quality gates run immediately before review and again after `PASS`.
- The journal can recover a commit when execution stops between commit creation
  and fast-forward.
- The primary worktree must stay clean and its HEAD cannot change while a task
  is running.

## Clean terminal output ✨

The default view displays only stages, results, elapsed time, and a log path on
failure. Raw Codex and test output is stored in runtime files instead of flooding
the terminal. `./ralph run --verbose` adds state and attempt counters, while
`--color never` disables ANSI colors but keeps emoji.

## Recovery

Running `./ralph run` again automatically resumes the active worktree. If the
process stopped after creating a commit but before fast-forwarding, Ralph finds
the commit by SHA and completes the transaction. Blocked work is never merged
into the primary branch.

## Testing Ralph

```bash
python3 -m unittest discover -s tests -v
```

The end-to-end test uses fake agents with real Git worktrees and commits, so the
test suite protects the application factory itself, not only generated code.
