from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from ralph_loop.errors import GateInfrastructureError, RuntimeBusyError, ScopeError
from ralph_loop.journal import Journal
from ralph_loop.manifest import Manifest
from ralph_loop.orchestrator import Orchestrator
from ralph_loop.ui import NullUI

from tests.helpers import Repo, SuccessfulAgent, run, task_payload


def active_task(repo: Repo):
    state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    worktree = Path(state["worktree"])
    manifest = Manifest.load(worktree / "docs/tasks/module/manifest.json", worktree)
    return state_path, state, manifest.get(state["taskId"])


class OrchestratorTests(unittest.TestCase):
    def prepare_active_cycle(self, repo, agent, through_gates=False):
        orchestrator = Orchestrator(repo.config, NullUI(), agent=agent)
        with orchestrator.journal.lock():
            orchestrator._preflight()
            root_manifest = Manifest.load(repo.config.manifest_path, repo.root)
            session = orchestrator._start_session(root_manifest.get("TASK-001"))
            manifest = orchestrator._manifest_for_session(session)
            task = manifest.get("TASK-001")
            orchestrator._planner(session, manifest, task)
            orchestrator._implementer(session, manifest, task)
            if through_gates:
                orchestrator._verifying(session, manifest, task)
        return orchestrator, session, manifest, task

    def test_end_to_end_creates_commit_and_fast_forwards(self):
        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()

        self.assertEqual(result, 0)
        self.assertEqual((repo.root / "src/value.txt").read_text(encoding="utf-8"), "ok\n")
        task = Manifest.load(repo.config.manifest_path, repo.root).get("TASK-001")
        self.assertEqual(task.status, "completed")
        self.assertNotEqual(before, run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip())
        self.assertEqual(run(["git", "status", "--porcelain"], repo.root).stdout, "")
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["ralphVersion"], "1.0.0")
        self.assertEqual(state["effectivePrd"], "docs/prd.md")
        self.assertNotIn("prd", state)
        self.assertNotIn("resumeStatus", state)
        self.assertEqual(state["cyclesUsed"], 1)
        self.assertEqual(
            state["lastReview"]["reviewsImplementationRunId"],
            state["currentCycle"]["implementationRunId"],
        )

    def test_terminal_output_is_compact_and_uses_role_icons(self):
        repo = Repo()
        self.addCleanup(repo.close)
        output = StringIO()
        from ralph_loop.ui import UI

        with redirect_stdout(output):
            result = Orchestrator(repo.config, UI(color="never"), agent=SuccessfulAgent()).run()
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        for marker in ("🧠", "🛠️", "🧪", "🔍", "✅", "📚", "PRD:", "All PRDs complete", "Active work:"):
            self.assertIn(marker, rendered)
        self.assertNotIn("fake agent", rendered)

    def test_run_cycle_limit_stops_before_starting_the_next_task(self):
        first = task_payload()
        second = task_payload(
            id="TASK-002",
            title="Second value",
            path="TASK-002-second-value",
            dependsOn=["TASK-001"],
        )
        repo = Repo(first)
        self.addCleanup(repo.close)
        second_dir = repo.root / "docs/tasks/module/TASK-002-second-value"
        second_dir.mkdir()
        (second_dir / "task.md").write_text("# TASK-002\n\n- AC-001\n", encoding="utf-8")
        data = json.loads(repo.config.manifest_path.read_text(encoding="utf-8"))
        data["tasks"].append(second)
        repo.config.manifest_path.write_text(json.dumps(data), encoding="utf-8")
        run(["git", "add", "-A"], repo.root)
        run(["git", "commit", "-m", "second task"], repo.root)

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run(max_cycles=1)

        self.assertEqual(result, 2)
        manifest = Manifest.load(repo.config.manifest_path, repo.root)
        self.assertEqual(manifest.get("TASK-001").status, "completed")
        self.assertEqual(manifest.get("TASK-002").status, "ready")

    def test_scope_violation_becomes_intervention_without_merge(self):
        class BadAgent(SuccessfulAgent):
            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "implementer":
                    (root / "forbidden.txt").write_text("nope\n", encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        result = Orchestrator(repo.config, NullUI(), agent=BadAgent()).run()
        self.assertEqual(result, 3)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "scope_violation")
        self.assertEqual(state["cyclesUsed"], 0)

    def test_agent_commit_is_never_merged(self):
        class CommittingAgent(SuccessfulAgent):
            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "implementer":
                    (root / "forbidden.txt").write_text("committed\n", encoding="utf-8")
                    run(["git", "add", "-A"], root)
                    run(["git", "commit", "-m", "agent commit"], root)
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()

        result = Orchestrator(repo.config, NullUI(), agent=CommittingAgent()).run()

        self.assertEqual(result, 3)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        self.assertFalse((repo.root / "forbidden.txt").exists())
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "scope_violation")
        self.assertNotEqual(
            run(["git", "rev-parse", "HEAD"], Path(state["worktree"])).stdout.strip(),
            state["baseSha"],
        )

    def test_quality_gate_commit_is_never_merged(self):
        class CommittingGates:
            def run_all(self, task, root, log_dir):
                run(["git", "add", "-A"], root)
                run(["git", "commit", "-m", "gate commit"], root)
                return []

        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()

        result = Orchestrator(
            repo.config,
            NullUI(),
            agent=SuccessfulAgent(),
            gates=CommittingGates(),
        ).run()

        self.assertEqual(result, 3)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "scope_violation")

    def test_final_commit_validation_rejects_a_forbidden_committed_path(self):
        repo = Repo()
        self.addCleanup(repo.close)
        orchestrator = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent())
        with orchestrator.journal.lock():
            orchestrator._preflight()
            manifest = Manifest.load(repo.config.manifest_path, repo.root)
            session = orchestrator._start_session(manifest.get("TASK-001"))
            worktree = Path(session.data["worktree"])
            task = orchestrator._manifest_for_session(session).get("TASK-001")
            (worktree / "forbidden.txt").write_text("no\n", encoding="utf-8")
            run(["git", "add", "-A"], worktree)
            run(["git", "commit", "-m", "forbidden"], worktree)
            commit_sha = run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()

            with self.assertRaisesRegex(ScopeError, "forbidden.txt"):
                orchestrator._validate_task_commit(session, task, commit_sha, worktree)

    def test_recovery_finishes_cleanup_after_merge_was_confirmed(self):
        repo = Repo()
        self.addCleanup(repo.close)
        orchestrator = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent())

        def crash_after_merge(session):
            raise RuntimeError("simulated cleanup crash")

        orchestrator._cleanup_session = crash_after_merge
        with self.assertRaisesRegex(RuntimeError, "cleanup crash"):
            orchestrator.run()

        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        interrupted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(interrupted["merged"])
        self.assertTrue(interrupted["active"])
        self.assertTrue(Path(interrupted["worktree"]).is_dir())

        self.assertEqual(
            Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run(), 0
        )
        recovered = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(recovered["active"])
        self.assertFalse(Path(recovered["worktree"]).exists())
        self.assertEqual(
            run(["git", "branch", "--list", recovered["branch"]], repo.root).stdout,
            "",
        )
        self.assertEqual((repo.root / "src/value.txt").read_text(encoding="utf-8"), "ok\n")

    def test_invalid_role_result_gets_three_retries_without_using_a_cycle(self):
        class InvalidPlanner(SuccessfulAgent):
            def __init__(self):
                self.calls = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "planner":
                    self.calls += 1
                    payload.pop("status")
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = InvalidPlanner()
        result = Orchestrator(repo.config, NullUI(), agent=agent).run()
        self.assertEqual(result, 3)
        self.assertEqual(agent.calls, 4)
        _, state, task = active_task(repo)
        self.assertEqual(state["cyclesUsed"], 0)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "technical_retries_exhausted")

    def test_manual_retry_resumes_the_same_planning_entitlement(self):
        class EventuallyValidPlanner(SuccessfulAgent):
            def __init__(self):
                self.calls = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "planner":
                    self.calls += 1
                    if self.calls <= 4:
                        payload.pop("status")
                        result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = EventuallyValidPlanner()
        orchestrator = Orchestrator(repo.config, NullUI(), agent=agent)
        self.assertEqual(orchestrator.run(), 3)
        self.assertEqual(orchestrator.retry("TASK-001", "planning", 1), 0)
        self.assertEqual(orchestrator.run(), 0)
        self.assertEqual(agent.calls, 5)

    def test_blocked_planner_can_resume_with_an_operator_note(self):
        class BlockedOncePlanner(SuccessfulAgent):
            def __init__(self):
                self.plans = 0
                self.contexts = []

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "planner":
                    self.plans += 1
                    self.contexts.append(context)
                    if self.plans == 1:
                        payload["status"] = "NEEDS_CLARIFICATION"
                        payload["summary"] = "Need an API decision"
                        result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = BlockedOncePlanner()
        orchestrator = Orchestrator(repo.config, NullUI(), agent=agent)

        self.assertEqual(orchestrator.run(), 3)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "needs_clarification")
        self.assertEqual(state["planningCredits"], 1)
        for field in ("blockReason", "lastFailure", "intervention"):
            self.assertNotIn(field, task.raw)
        self.assertNotIn(
            "--prd",
            [item for action in state["intervention"]["nextActions"] for item in action],
        )

        from ralph_loop.cli import _status
        from ralph_loop.ui import UI

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(_status(repo.config, UI(color="never")), 0)
        self.assertIn(
            "next: ralph retry TASK-001 --stage planning --attempts 1",
            output.getvalue(),
        )

        self.assertEqual(
            orchestrator.retry(
                "TASK-001", "planning", 1, "Use the stable API contract"
            ),
            0,
        )
        self.assertEqual(orchestrator.run(), 0)
        self.assertEqual(agent.plans, 2)
        self.assertIn("Use the stable API contract", agent.contexts[1])

    def test_blocked_implementer_releases_cycle_and_can_resume(self):
        class BlockedOnceImplementer(SuccessfulAgent):
            def __init__(self):
                self.implementations = 0
                self.contexts = []

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "implementer":
                    self.implementations += 1
                    self.contexts.append(context)
                    if self.implementations == 1:
                        payload["status"] = "BLOCKED"
                        payload["summary"] = "Need the expected format"
                        payload["acceptanceCriteria"][0]["status"] = "FAIL"
                        result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = BlockedOnceImplementer()
        orchestrator = Orchestrator(repo.config, NullUI(), agent=agent)

        self.assertEqual(orchestrator.run(), 3)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "role_blocked")
        self.assertEqual(state["cyclesUsed"], 0)
        self.assertIsNone(state["currentCycle"])

        self.assertEqual(
            orchestrator.retry(
                "TASK-001", "implementation", 1, "Write one line ending in LF"
            ),
            0,
        )
        self.assertEqual(orchestrator.run(), 0)
        self.assertEqual(agent.implementations, 2)
        self.assertIn("Write one line ending in LF", agent.contexts[1])
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["cyclesUsed"], 1)

    def test_gate_infrastructure_retries_same_candidate_without_extra_cycle(self):
        class FlakyGates:
            def __init__(self):
                self.calls = 0

            def run_all(self, task, root, log_dir):
                self.calls += 1
                if self.calls <= 3:
                    raise GateInfrastructureError("temporary gate outage")
                return []

        repo = Repo()
        self.addCleanup(repo.close)
        gates = FlakyGates()
        result = Orchestrator(
            repo.config, NullUI(), agent=SuccessfulAgent(), gates=gates
        ).run()
        self.assertEqual(result, 0)
        self.assertEqual(gates.calls, 5)  # four initial-gate calls and one final gate
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["cyclesUsed"], 1)

    def test_failed_gates_still_receive_review_and_cannot_pass(self):
        class FailedCandidateAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "implementer":
                    (root / "src/value.txt").write_text("bad\n", encoding="utf-8")
                    payload["status"] = "VERIFICATION_FAILED"
                    payload["acceptanceCriteria"][0]["status"] = "FAIL"
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                elif role == "reviewer":
                    self.reviews += 1
                    payload["status"] = "FAIL"
                    payload["acceptanceCriteria"][0]["status"] = "FAIL"
                    payload["findings"] = [{
                        "id": "REV-001", "severity": "high", "file": "src/value.txt",
                        "description": "Wrong value", "expectedBehavior": "Value is ok",
                        "status": "open",
                    }]
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        task = task_payload()
        task["limits"]["cycles"] = 1
        repo = Repo(task)
        self.addCleanup(repo.close)
        agent = FailedCandidateAgent()
        result = Orchestrator(repo.config, NullUI(), agent=agent).run()
        self.assertEqual(result, 3)
        self.assertEqual(agent.reviews, 1)
        _, state, current = active_task(repo)
        self.assertEqual(state["cyclesUsed"], 1)
        self.assertIsNone(state["pendingReview"])
        self.assertEqual(current.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "cycle_budget_exhausted")

    def test_reviewer_pass_for_failed_candidate_is_retried_then_intervenes(self):
        class InvalidPassAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "implementer":
                    (root / "src/value.txt").write_text("bad\n", encoding="utf-8")
                    payload["status"] = "VERIFICATION_FAILED"
                    payload["acceptanceCriteria"][0]["status"] = "FAIL"
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                elif role == "reviewer":
                    self.reviews += 1
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = InvalidPassAgent()
        self.assertEqual(Orchestrator(repo.config, NullUI(), agent=agent).run(), 3)
        self.assertEqual(agent.reviews, 4)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["stage"], "review")
        self.assertIsNotNone(state["pendingReview"])
        self.assertEqual(state["cyclesUsed"], 1)

    def test_needs_replan_grants_exactly_one_planning_credit(self):
        class ReplanningAgent(SuccessfulAgent):
            def __init__(self):
                self.plans = 0
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "planner":
                    self.plans += 1
                if role == "reviewer":
                    self.reviews += 1
                    if self.reviews == 1:
                        payload["status"] = "NEEDS_REPLAN"
                        payload["acceptanceCriteria"][0]["status"] = "FAIL"
                        result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = ReplanningAgent()
        self.assertEqual(Orchestrator(repo.config, NullUI(), agent=agent).run(), 0)
        self.assertEqual(agent.plans, 2)
        self.assertEqual(agent.reviews, 2)
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["planningCredits"], 0)
        self.assertEqual(state["cyclesUsed"], 2)

    def test_identical_diff_and_findings_stop_for_no_progress(self):
        class StalledAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "reviewer":
                    self.reviews += 1
                    payload["status"] = "FAIL"
                    payload["acceptanceCriteria"][0]["status"] = "FAIL"
                    payload["findings"] = [{
                        "id": "REV-001", "severity": "high", "file": "src/value.txt",
                        "description": "Still wrong", "expectedBehavior": "Fix it",
                        "status": "open",
                    }]
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = StalledAgent()
        self.assertEqual(Orchestrator(repo.config, NullUI(), agent=agent).run(), 3)
        self.assertEqual(agent.reviews, 2)
        _, state, task = active_task(repo)
        self.assertEqual(task.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "no_progress")

    def test_extend_adds_runtime_cycle_without_changing_manifest_limit(self):
        class OneFixAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                payload = super().run(
                    role, task, root, result_path, log_path, context, invocation
                )
                if role == "reviewer":
                    self.reviews += 1
                    if self.reviews == 1:
                        payload["status"] = "FAIL"
                        payload["acceptanceCriteria"][0]["status"] = "FAIL"
                        payload["findings"] = [{
                            "id": "REV-001", "severity": "high", "file": "src/value.txt",
                            "description": "Needs one pass", "expectedBehavior": "Fixed",
                            "status": "open",
                        }]
                    else:
                        payload["findings"] = [{
                            "id": "REV-001", "severity": "high", "file": "src/value.txt",
                            "description": "Needs one pass", "expectedBehavior": "Fixed",
                            "status": "resolved",
                        }]
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        task = task_payload()
        task["limits"]["cycles"] = 1
        repo = Repo(task)
        self.addCleanup(repo.close)
        agent = OneFixAgent()
        orchestrator = Orchestrator(repo.config, NullUI(), agent=agent)
        self.assertEqual(orchestrator.run(), 3)
        self.assertEqual(orchestrator.extend("TASK-001", 1), 0)
        self.assertEqual(orchestrator.run(), 0)
        final = Manifest.load(repo.config.manifest_path, repo.root).get("TASK-001")
        self.assertEqual(final.raw["limits"]["cycles"], 1)
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["cycleGrants"], 1)
        self.assertEqual(state["cyclesUsed"], 2)

    def test_mutating_quality_gate_becomes_scope_intervention(self):
        task = task_payload()
        task["qualityGates"] = [{
            "name": "bad gate",
            "command": [
                "python3", "-c",
                "from pathlib import Path; Path('gate-side-effect.txt').write_text('bad')",
            ],
            "timeoutSeconds": 10,
        }]
        repo = Repo(task)
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()
        self.assertEqual(result, 3)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        _, state, current = active_task(repo)
        self.assertEqual(current.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "scope_violation")

    def test_preflight_reconciles_manifest_from_authoritative_session(self):
        repo = Repo()
        self.addCleanup(repo.close)
        agent = SuccessfulAgent()
        orchestrator, session, manifest, task = self.prepare_active_cycle(repo, agent)
        self.assertEqual(session.data["workflowStatus"], "verifying")
        task.status = "implementing"  # simulate a crash before manifest projection
        manifest.save()

        self.assertEqual(orchestrator.run(), 0)
        final = Manifest.load(repo.config.manifest_path, repo.root).get("TASK-001")
        self.assertEqual(final.status, "completed")

    def test_new_session_pins_task_specific_prd(self):
        repo = Repo()
        self.addCleanup(repo.close)
        task_prd = repo.root / "docs" / "task-prd.md"
        task_prd.write_text("# Task PRD\n", encoding="utf-8")
        manifest_data = json.loads(repo.config.manifest_path.read_text(encoding="utf-8"))
        manifest_data["tasks"][0]["prd"] = "docs/task-prd.md"
        repo.config.manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
        run(["git", "add", "-A"], repo.root)
        run(["git", "commit", "-m", "task PRD"], repo.root)

        orchestrator = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent())
        self.assertEqual(orchestrator.run(), 0)
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["effectivePrd"], "docs/task-prd.md")

    def test_recovery_does_not_repeat_an_already_accepted_review(self):
        class CountingAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                if role == "reviewer":
                    self.reviews += 1
                return super().run(
                    role, task, root, result_path, log_path, context, invocation
                )

        repo = Repo()
        self.addCleanup(repo.close)
        agent = CountingAgent()
        orchestrator, session, manifest, task = self.prepare_active_cycle(
            repo, agent, through_gates=True
        )
        with orchestrator.journal.lock():
            orchestrator._reviewer(session, manifest, task)
        self.assertEqual(agent.reviews, 1)
        self.assertEqual(session.data["workflowStatus"], "finalizing")
        task.status = "in_review"  # simulate interruption before manifest projection
        manifest.save()

        self.assertEqual(orchestrator.run(), 0)
        self.assertEqual(agent.reviews, 1)

    def test_recovery_consumes_persisted_role_result_without_reinvocation(self):
        class CountingAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context="", invocation=1):
                if role == "reviewer":
                    self.reviews += 1
                return super().run(
                    role, task, root, result_path, log_path, context, invocation
                )

        repo = Repo()
        self.addCleanup(repo.close)
        agent = CountingAgent()
        orchestrator, session, manifest, task = self.prepare_active_cycle(
            repo, agent, through_gates=True
        )
        with orchestrator.journal.lock():
            orchestrator._run_role_once(
                session,
                task,
                "reviewer",
                [f"{task.task_dir}/review.md"],
                "simulated accepted review before transition",
                None,
            )
        self.assertEqual(agent.reviews, 1)
        self.assertEqual(session.data["unconsumedRoleRun"]["role"], "reviewer")

        self.assertEqual(orchestrator.run(), 0)
        self.assertEqual(agent.reviews, 1)
        state_path = next((repo.root / ".ralph/runtime/sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["unconsumedRoleRun"])

    def test_external_candidate_change_requires_intervention(self):
        repo = Repo()
        self.addCleanup(repo.close)
        agent = SuccessfulAgent()
        orchestrator, session, manifest, task = self.prepare_active_cycle(
            repo, agent, through_gates=True
        )
        worktree = Path(session.data["worktree"])
        (worktree / "src/value.txt").write_text("external\n", encoding="utf-8")

        self.assertEqual(orchestrator.run(), 3)
        _, state, current = active_task(repo)
        self.assertEqual(current.status, "needs_intervention")
        self.assertEqual(state["intervention"]["reason"], "candidate_changed")
        self.assertIsNotNone(state["pendingReview"])

    def test_runtime_lock_rejects_a_second_mutating_process(self):
        repo = Repo()
        self.addCleanup(repo.close)
        first = Journal(repo.config.runtime_path)
        second = Journal(repo.config.runtime_path)
        with first.lock():
            with self.assertRaises(RuntimeBusyError):
                with second.lock():
                    pass


if __name__ == "__main__":
    unittest.main()
