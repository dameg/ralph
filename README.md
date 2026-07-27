# Ralph Loop v3 🤖

[![CI](https://github.com/dameg/ralph/actions/workflows/ci.yml/badge.svg)](https://github.com/dameg/ralph/actions/workflows/ci.yml)

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

### Run a PRD generated from a conversation

Generate and save a PRD with your preferred tool, for example `to-prd` in
Cursor, then point Ralph at it for that run:

```bash
./ralph run --prd docs/prds/checkout.md
```

The override is transient: it does not modify `.ralph/config.json`. It must be
provided again when resuming an active task. Ralph records the selected PRD in
the session and refuses a resume with a different one. The manifest and each
`task.md` remain the implementation contract; a task-level `prd` field takes
precedence when you are coordinating multiple PRDs.

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

An optional task-level `prd` path overrides the default PRD from
`.ralph/config.json`. This lets one manifest coordinate tasks generated from
multiple PRDs.

Statuses form an explicit state machine:

```text
ready → planning → planned → implementing → in_review → completed
            ↑              ↖ needs_changes ↙       │
            └──────── needs_replan ─────────────────┘
```

The `blocked` and `failed` states stop the session without merging partial code.
The isolated branch and worktree remain available for inspection, and their
locations are shown in the terminal.

## Role-aware model routing

Ralph uses an adaptive model profile for each role. Initial attempts use Terra
to keep routine work efficient. Repeated attempts automatically escalate to Sol
with higher reasoning effort:

| Role | Initial profile | Escalation |
|---|---|---|
| Planner | `gpt-5.6-terra`, medium | Sol/high after 1 failed attempt |
| Implementer | `gpt-5.6-terra`, medium | Sol/high after 2 failed attempts |
| Reviewer | `gpt-5.6-terra`, high | Sol/high after 1 failed attempt |

The defaults work even for configuration files created by earlier Ralph
versions. Override them under `agent.roles` in `.ralph/config.json`:

```json
{
  "agent": {
    "roles": {
      "planner": {
        "model": "gpt-5.6-terra",
        "reasoningEffort": "medium",
        "escalateAfterAttempts": 1,
        "escalationModel": "gpt-5.6-sol",
        "escalationReasoningEffort": "high"
      }
    }
  }
}
```

Use `ralph run --verbose` to see the selected model, reasoning effort, and
whether the current attempt was escalated. The same data is written to the
runtime journal.

## Multiple PRDs in one queue

Keep all PRDs and their tasks in one versioned manifest when they belong to the
same repository:

```text
docs/
├── prds/
│   ├── 01-authentication.md
│   ├── 02-billing.md
│   └── 03-reporting.md
└── tasks/product/
    ├── manifest.json
    ├── auth/AUTH-001-.../task.md
    ├── billing/BILL-001-.../task.md
    └── reporting/REPORT-001-.../task.md
```

Each task points to its source PRD:

```json
{
  "id": "BILL-001",
  "prd": "docs/prds/02-billing.md",
  "path": "billing/BILL-001-create-invoice-model",
  "dependsOn": ["AUTH-003"]
}
```

Ralph selects the first executable task in manifest order and checks every
dependency before starting it. To enforce strict module order, make the first
task of the next PRD depend on the final integration task of the previous PRD:

```text
AUTH-001 → AUTH-002 → AUTH-003
                         ↓
BILL-001 → BILL-002 → BILL-003
                         ↓
REPORT-001 → REPORT-002
```

After committing the PRDs, task descriptions, and manifest, one command runs
the complete queue:

```bash
ralph run
```

If a task becomes blocked, Ralph does not silently skip into a later module when
that module depends on it. Resolve the blocker in the preserved worktree, then
run `ralph run` again.

## Determinism and safety

- Every role result is checked against a JSON Schema and then validated for
  complete evidence covering every `AC-*` criterion.
- The planner may modify only `plan.md`, the reviewer only `review.md`, and the
  implementer only `progress.md` plus paths allowed by the task contract.
- Any out-of-scope modification stops the workflow immediately.
- Role processes and quality gates cannot create commits or move the task
  branch. Finalization requires exactly one orchestrator-owned commit whose
  parent is the recorded base SHA.
- Quality gates run without a shell, with a timeout and fixed `TZ`, locale, and
  `PYTHONHASHSEED` values.
- Agent network access is disabled by default. It can be enabled explicitly in
  `.ralph/config.json` when a task genuinely requires it.
- The same quality gates run immediately before review and again after `PASS`.
- The journal can recover a commit when execution stops between commit creation
  and fast-forward, and can finish cleanup when execution stops after the
  fast-forward.
- The primary worktree must stay clean and its HEAD cannot change while a task
  is running.

Agent process failures such as timeouts, startup failures, or non-zero CLI exits
stop the run without consuming a planning, implementation, or review attempt.
Invalid role results still consume an attempt because they are semantic
failures rather than infrastructure failures.

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
