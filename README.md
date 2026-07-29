# Ralph Loop 1.0.0 🤖

[![CI](https://github.com/dameg/ralph/actions/workflows/ci.yml/badge.svg)](https://github.com/dameg/ralph/actions/workflows/ci.yml)

A deterministic, cycle-based Codex orchestrator for implementing complete
modules from a PRD. Models do the creative work; Ralph owns state transitions,
scope enforcement, quality gates, review obligations, recovery, and Git
finalization.

## Workflow

```text
📋 task contract
   ↓
🧠 Planner
   ↓
┌────────────── cycle (default limit: 5) ──────────────┐
│ 🛠️ Implementer → 🧪 quality gates → 🔍 Reviewer     │
└───────────────────────────┬──────────────────────────┘
                            ├─ FAIL → next cycle
                            ├─ NEEDS_REPLAN → Planner → next cycle
                            └─ PASS → final gates → commit → fast-forward
```

Every valid, non-blocking implementer result creates a candidate and a durable
review obligation. This includes `IN_PROGRESS`, `VERIFICATION_FAILED`, and
candidates whose deterministic quality gates fail. A cycle cannot be abandoned
because a separate review budget ran out: review has no independent business
limit.

Each task runs on an isolated branch in a separate Git worktree. The primary
branch moves only after reviewer `PASS`, successful final gates, and a confirmed
commit. Runtime state, results, logs, lineage, and the append-only journal live
under `.ralph/runtime/` and are not committed.

## Versioned contracts

Ralph 1.0 uses one clean contract generation:

- configuration must use `version: 1`;
- manifests must use `version: 1`;
- runtime sessions must use `version: 1`;
- stage attempt limits, `maxIterations`, and `failed` no longer exist;
- no migration or reset command is provided.

Sessions record the Ralph package version and effective task PRD. Runtime-only
interventions, failures, and recovery context remain in `.ralph/runtime`; the
manifest contains only the durable task contract and projected task status.
Other contract versions are rejected rather than migrated.

## Getting started

Requirements: Python 3.9+, Git, and an authenticated Codex CLI.

```bash
./ralph init --prd docs/my-module-prd.md \
  --manifest docs/tasks/my-module/manifest.json

# Complete and commit the PRD, manifest, and task.md files.
./ralph doctor
./ralph status
./ralph run
```

Installing a system-wide command is optional:

```bash
python3 -m pip install -e .
ralph run
```

## CLI reference

Run `ralph` after installation or use the repository-local `./ralph` wrapper.
Every command must run from inside the target Git repository.

| Command | Purpose |
| --- | --- |
| `ralph init` | Create the v1 configuration, manifest, PRD, prompts, and schemas. |
| `ralph doctor` | Validate Git, Codex, files, templates, and the manifest contract. |
| `ralph status` | Show task progress and the active session or intervention. |
| `ralph run` | Start or safely resume the planner/implementer/reviewer loop. |
| `ralph retry` | Grant more role invocations after an intervention. |
| `ralph extend` | Grant more task cycles after budget exhaustion or no progress. |

Global discovery commands:

```bash
ralph --help
ralph --version
ralph run --help
```

### `ralph init`

```text
ralph init [--manifest PATH] [--prd PATH] [--force]
```

Initializes Ralph in the current repository. Default paths are
`docs/tasks/manifest.json` and `docs/prd.md`.

```bash
# Use the default paths.
ralph init

# Select the project PRD and manifest paths.
ralph init \
  --prd docs/prds/platform.md \
  --manifest docs/tasks/platform/manifest.json

# Regenerate Ralph configuration and packaged templates.
ralph init --force
```

`--force` overwrites `.ralph/config.json`, `.ralph/.gitignore`, prompts, and
schemas. It does not overwrite an existing project PRD or manifest.

### `ralph doctor`

```text
ralph doctor [--color {auto,always,never}]
```

Checks command availability, repository state, Git identity and branch prefix,
the configured PRD and manifest, task descriptions, prompts, and schemas.

```bash
ralph doctor
ralph doctor --color never
```

### `ralph status`

```text
ralph status [--color {auto,always,never}]
```

Shows every task status, total progress, the active task and cycle usage, a
pending review obligation, and session-backed intervention actions.

```bash
ralph status
ralph status --color always
```

### `ralph run`

```text
ralph run [--max-cycles N] [--verbose] [--color {auto,always,never}]
```

Starts the next executable task or resumes the active session. `--max-cycles`
limits newly admitted cycles for this invocation and must be positive. Without
the option, Ralph uses `maxCyclesPerRun` from the configuration. `--verbose`
shows role routing, attempts, cycle usage, and detailed execution output.

```bash
# Start or resume with the configured run limit.
ralph run

# Admit at most four new cycles during this invocation.
ralph run --max-cycles 4

# Show detailed, non-colored output suitable for a CI log.
ralph run --verbose --color never
```

### `ralph retry`

```text
ralph retry TASK_ID --stage STAGE --attempts N [--note TEXT]
  [--color {auto,always,never}]
```

Grants positive additional invocation attempts to the stage recorded by the
active intervention. `STAGE` must be one of `planning`, `implementation`,
`gates`, `review`, or `final-gates`. An optional note is passed once as recovery
context and does not modify the PRD or task contract.

```bash
ralph retry CORE-003 --stage review --attempts 1
ralph retry CORE-004 --stage planning --attempts 1 \
  --note "Use the approved public API from the architecture decision"
```

Use `retry` for a recoverable role, gate, candidate, or scope intervention. It
cannot replace a cycle extension requested by Ralph.

### `ralph extend`

```text
ralph extend TASK_ID --cycles N [--color {auto,always,never}]
```

Adds a positive number of runtime-only cycles when the active intervention is
`cycle_budget_exhausted` or `no_progress`. The manifest cycle limit remains
unchanged.

```bash
ralph extend CORE-003 --cycles 1
ralph run
```

### Color and exit codes

`--color auto` follows terminal detection, `always` forces ANSI colors, and
`never` disables them. If omitted, operational commands use `ui.color` from the
configuration.

| Exit code | Meaning |
| --- | --- |
| `0` | Command succeeded, or `run` found no tasks/all tasks completed. |
| `1` | Configuration, validation, environment, Git, agent, or internal error. |
| `2` | `run` reached its invocation cycle limit safely; argparse also uses `2` for invalid CLI syntax. |
| `3` | Work remains but no task can proceed, usually because of blockers or an active intervention. |

## Configuration v1

The generated `.ralph/config.json` includes:

```json
{
  "version": 1,
  "manifest": "docs/tasks/manifest.json",
  "prd": "docs/prd.md",
  "runtime": ".ralph/runtime",
  "maxCyclesPerRun": 30,
  "retryPolicy": {
    "technicalRetries": 3
  }
}
```

`maxCyclesPerRun` limits newly admitted cycles during one command. Once a cycle
starts, Ralph always carries it through gates and review or into an actionable
`needs_intervention` state. Override the run cap with:

```bash
ralph run --max-cycles 4
```

`technicalRetries: 3` means one initial invocation plus at most three automatic
retries for each operation: planning, implementation, gates, review, and final
gates. Technical failures do not consume task cycles.

## Manifest v1

A complete example is available in
[`examples/manifest.json`](examples/manifest.json), with a task template in
[`examples/task.md`](examples/task.md).

Every task defines stable IDs, dependencies, explicit `AC-*` criteria, enforced
allowed paths, deterministic quality gates, and one cycle limit. Omitting
`limits` uses the default of five cycles:

```json
{
  "version": 1,
  "taskWorkspace": "docs/tasks/example-module",
  "tasks": [
    {
      "id": "CORE-001",
      "title": "Add the capability",
      "status": "ready",
      "path": "CORE-001-add-capability",
      "dependsOn": [],
      "contract": {
        "acceptanceCriteria": [
          {"id": "AC-001", "description": "Behavior is observable and tested"}
        ],
        "allowedPaths": ["src/**", "tests/**"],
        "nonGoals": []
      },
      "qualityGates": [
        {
          "name": "tests",
          "command": ["python3", "-m", "unittest", "discover", "-s", "tests"],
          "timeoutSeconds": 300
        }
      ],
      "limits": {"cycles": 5}
    }
  ]
}
```

An optional task-level `prd` overrides the configured PRD. Tasks without that
field use the project-level `prd` from `.ralph/config.json`. The effective PRD
is pinned in runtime session state and reused by `run`, `retry`, and `extend`.

## State machine

```text
ready → planning → planned → implementing → verifying → in_review
                    ↑                                  │
                    ├──── needs_changes ←──── FAIL ────┤
       needs_replan └──── NEEDS_REPLAN ────────────────┤
                                                       ↓ PASS
                                                  finalizing
                                                       ↓
                                                   completed
```

`blocked` represents dependency scheduling. A planner `BLOCKED` or
`NEEDS_CLARIFICATION`, and an implementer `BLOCKED`, now become an actionable
`needs_intervention` without consuming the pending planning credit or a task
cycle.
`needs_intervention` preserves the active worktree and records a reason,
resume stage, details, and exact next actions.

## Review guarantees and lineage

When the implementer returns a valid non-blocking result, Ralph atomically
consumes a cycle and records a `pendingReview` containing:

- the cycle and implementation run IDs;
- the implementation status;
- a digest of the reviewable diff;
- deterministic gate status and evidence.

Review can close that obligation only with a valid `PASS`, `FAIL`, or
`NEEDS_REPLAN` for the same candidate digest. `PASS` is forbidden unless the
implementation is complete, all acceptance evidence passes, gates pass, and no
finding remains open.

Lineage records which review led to an implementation and which subsequent
review evaluated it. Existing `REV-*` findings must remain present as `open` or
`resolved`. If two consecutive reviews return the same open finding IDs for an
unchanged candidate digest, Ralph stops early with `no_progress`.

## Intervention commands

Operational retries and business-budget extensions are deliberately separate:

```bash
ralph retry CORE-003 --stage review --attempts 1
ralph retry CORE-004 --stage planning --attempts 1 \
  --note "Use the approved public API from the architecture decision"
ralph extend CORE-003 --cycles 1
```

`retry` grants additional invocations without changing the cycle budget.
`extend` grants runtime-only cycles without modifying the manifest contract.
Both require an active task in `needs_intervention`, validate the requested
stage, and append an audit event to the journal. An optional `--note` is passed
once to the next role invocation as recovery context; it does not change the
task contract.

Scope violations and externally changed candidates must be repaired manually in
the preserved worktree before retry is accepted.

## Role-aware model routing

Initial invocations use Terra; repeated invocations can escalate to Sol. Routing
uses actual role invocation numbers rather than consumed cycles, so invalid
responses can escalate without exhausting business budget. Override profiles
under `agent.roles` in `.ralph/config.json`.

Use `ralph run --verbose` to see cycle usage, model selection, reasoning effort,
and escalation. `ralph status` shows active cycles, pending review, intervention
details, and suggested actions.

## Progress and usage reporting

Interactive terminals show a spinner and elapsed timer while a role or quality
gate runs. Ralph announces the effective PRD for each task (`task.prd` takes
precedence over the configured value), then reports task and overall progress
after every completed task. Final completion includes one line per PRD with
active work time and token usage.

Token usage is collected from Codex JSONL output as input, output, and cached
input tokens. Custom agent commands, or Codex versions that do not provide
usage events, continue normally and display `usage unavailable`.

## Determinism and safety

- Role JSON is validated both by the Codex output schema and in-process against
  the complete field contract, including nested evidence, verification, and
  reviewer findings.
- Configuration and manifests reject unknown fields, unsafe paths, and booleans
  supplied where integer limits are required.
- Planner, implementer, and reviewer file scopes are enforced independently.
- Roles and quality gates may not change the task branch HEAD. Before merge,
  Ralph independently verifies that the final commit has exactly one parent,
  is based on the recorded base SHA, and contains only permitted paths.
- Quality gates run without a shell, with timeouts and a stable environment.
- Deterministic gate failures still receive independent review.
- Infrastructure failures retry the same operation without spending cycles.
- Candidate digests exclude workflow artifacts but include code paths, content,
  file type, and mode.
- An exclusive runtime lock prevents concurrent `run`, `retry`, or `extend`
  processes from mutating one session.
- Session state is authoritative and reconciles the manifest after interruption.
- Confirmed commits, fast-forwards, and interrupted post-merge cleanup remain
  recoverable after process failure.

## Testing Ralph

```bash
python3 -m unittest discover -s tests -v
```

The end-to-end suite uses fake agents with real Git worktrees and commits. It
covers mandatory review, cycle caps, technical retry, session-only intervention,
lineage, no-progress detection, strict v1 contracts, forbidden agent and gate
commits, crash recovery, and finalization.
