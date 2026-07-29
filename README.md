# Ralph Loop v4 🤖

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

## Breaking changes

Ralph v4 intentionally provides no backward compatibility:

- configuration must use `version: 2`;
- manifests must use `version: 4`;
- old runtime sessions are rejected;
- stage attempt limits, `maxIterations`, and `failed` no longer exist;
- no migration or reset command is provided.

Prepare or reset existing projects manually before running v4.

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

## Configuration v2

The generated `.ralph/config.json` includes:

```json
{
  "version": 2,
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

## Manifest v4

A complete example is available in
[`examples/manifest.json`](examples/manifest.json), with a task template in
[`examples/task.md`](examples/task.md).

Every task defines stable IDs, dependencies, explicit `AC-*` criteria, enforced
allowed paths, deterministic quality gates, and one cycle limit. Omitting
`limits` uses the default of five cycles:

```json
{
  "version": 4,
  "workflow": "planner-implementer-reviewer",
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

An optional task-level `prd` overrides the configured PRD. The transient run
override remains available:

```bash
ralph run --prd docs/prds/checkout.md
```

Use the same `--prd` when resuming that active session. Suggested recovery
commands include it automatically.

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

`blocked` represents dependencies or an explicit role blocker.
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
ralph extend CORE-003 --cycles 1
```

`retry` grants additional invocations without changing the cycle budget.
`extend` grants runtime-only cycles without modifying the manifest contract.
Both require an active task in `needs_intervention`, validate the requested
stage, and append an audit event to the journal.

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

## Determinism and safety

- Role JSON is schema-validated and checked for complete `AC-*` evidence.
- Planner, implementer, and reviewer file scopes are enforced independently.
- Quality gates run without a shell, with timeouts and a stable environment.
- Deterministic gate failures still receive independent review.
- Infrastructure failures retry the same operation without spending cycles.
- Candidate digests exclude workflow artifacts but include code paths, content,
  file type, and mode.
- An exclusive runtime lock prevents concurrent `run`, `retry`, or `extend`
  processes from mutating one session.
- Session state is authoritative and reconciles the manifest after interruption.
- Confirmed commits and fast-forwards remain recoverable after process failure.

## Testing Ralph

```bash
python3 -m unittest discover -s tests -v
```

The end-to-end suite uses fake agents with real Git worktrees and commits. It
covers mandatory review, cycle caps, technical retry, intervention, lineage,
no-progress detection, strict v4 contracts, and finalization.
