from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from ralph_loop.errors import AgentInfrastructureError, ScopeError
from ralph_loop.manifest import Manifest
from ralph_loop.orchestrator import Orchestrator
from ralph_loop.ui import NullUI

from tests.helpers import Repo, SuccessfulAgent, run, task_payload


class OrchestratorTests(unittest.TestCase):
    def test_end_to_end_creates_commit_and_fast_forwards(self):
        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()

        self.assertEqual(result, 0)
        self.assertEqual((repo.root / "src" / "value.txt").read_text(encoding="utf-8"), "ok\n")
        manifest = Manifest.load(repo.config.manifest_path, repo.root)
        self.assertEqual(manifest.get("TASK-001").status, "completed")
        after = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        self.assertNotEqual(before, after)
        self.assertEqual(run(["git", "status", "--porcelain"], repo.root).stdout, "")
        branches = run(["git", "branch", "--format=%(refname:short)"], repo.root).stdout.splitlines()
        self.assertNotIn("ralph/task-001", branches)

    def test_terminal_output_is_compact_and_uses_role_icons(self):
        repo = Repo()
        self.addCleanup(repo.close)
        output = StringIO()
        from ralph_loop.ui import UI

        with redirect_stdout(output):
            result = Orchestrator(
                repo.config, UI(color="never"), agent=SuccessfulAgent()
            ).run()
        rendered = output.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("🧠", rendered)
        self.assertIn("🛠️", rendered)
        self.assertIn("🧪", rendered)
        self.assertIn("🔍", rendered)
        self.assertIn("✅", rendered)
        self.assertNotIn("fake agent", rendered)
        self.assertNotIn("Preparing worktree", rendered)

    def test_exact_iteration_budget_reports_success(self):
        repo = Repo()
        self.addCleanup(repo.close)
        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run(
            max_iterations=3
        )
        self.assertEqual(result, 0)

    def test_satisfied_dependency_is_promoted_inside_isolated_worktree(self):
        completed = task_payload(status="completed")
        second = task_payload(
            id="TASK-002",
            title="Dependent task",
            path="TASK-001-create-value",
            status="blocked",
            dependsOn=["TASK-001"],
        )
        repo = Repo(completed)
        self.addCleanup(repo.close)
        manifest_path = repo.config.manifest_path
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["tasks"].append(second)
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        run(["git", "add", "-A"], repo.root)
        run(["git", "commit", "-m", "add dependent task"], repo.root)

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run(
            max_iterations=3
        )

        self.assertEqual(result, 0)
        final = Manifest.load(manifest_path, repo.root)
        self.assertEqual(final.get("TASK-002").status, "completed")

    def test_implementer_scope_violation_stops_without_merge(self):
        class BadAgent(SuccessfulAgent):
            def run(self, role, task, root, result_path, log_path, context=""):
                payload = super().run(role, task, root, result_path, log_path, context)
                if role == "implementer":
                    (root / "forbidden.txt").write_text("nope\n", encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        with self.assertRaisesRegex(ScopeError, "forbidden.txt"):
            Orchestrator(repo.config, NullUI(), agent=BadAgent()).run()
        after = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        self.assertEqual(before, after)
        self.assertFalse((repo.root / "forbidden.txt").exists())

    def test_precommit_completed_state_is_recovered(self):
        repo = Repo()
        self.addCleanup(repo.close)
        first = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent())
        self.assertEqual(first.run(max_iterations=2), 2)
        state_path = next((repo.root / ".ralph" / "runtime" / "sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worktree = Path(state["worktree"])
        manifest = Manifest.load(
            worktree / "docs" / "tasks" / "module" / "manifest.json", worktree
        )
        manifest.get("TASK-001").status = "completed"
        manifest.save()

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()

        self.assertEqual(result, 0)
        final = Manifest.load(repo.config.manifest_path, repo.root)
        self.assertEqual(final.get("TASK-001").status, "completed")

    def test_unrecorded_commit_is_recovered_and_merged(self):
        repo = Repo()
        self.addCleanup(repo.close)
        orchestrator = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent())
        self.assertEqual(orchestrator.run(max_iterations=2), 2)
        state_path = next((repo.root / ".ralph" / "runtime" / "sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worktree = Path(state["worktree"])
        manifest = Manifest.load(
            worktree / "docs" / "tasks" / "module" / "manifest.json", worktree
        )
        task = manifest.get("TASK-001")
        SuccessfulAgent().run(
            "reviewer",
            task,
            worktree,
            state_path.parent / "manual-result.json",
            state_path.parent / "manual.log",
        )
        task.status = "completed"
        manifest.save()
        committed = orchestrator.git.commit_all(worktree, "TASK-001: simulated interrupted commit")

        result = Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()

        self.assertEqual(result, 0)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), committed)
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(final_state["active"])
        self.assertTrue(final_state["merged"])

    def test_completed_merge_with_pending_cleanup_is_recovered(self):
        class CrashBeforeCleanup(Orchestrator):
            def _cleanup_session(self, session):
                raise RuntimeError("simulated crash before cleanup")

        repo = Repo()
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            CrashBeforeCleanup(
                repo.config, NullUI(), agent=SuccessfulAgent()
            ).run()

        state_path = next(
            (repo.root / ".ralph" / "runtime" / "sessions").glob("*/state.json")
        )
        interrupted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(interrupted["merged"])
        self.assertTrue(interrupted["active"])

        result = Orchestrator(
            repo.config, NullUI(), agent=SuccessfulAgent()
        ).run()

        self.assertEqual(result, 0)
        recovered = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(recovered["merged"])
        self.assertFalse(recovered["active"])
        self.assertFalse(Path(recovered["worktree"]).exists())
        branches = run(
            ["git", "branch", "--format=%(refname:short)"], repo.root
        ).stdout.splitlines()
        self.assertNotIn("ralph/task-001", branches)

    def test_agent_infrastructure_failure_does_not_consume_attempt(self):
        class OfflineAgent:
            def run(self, role, task, root, result_path, log_path, context=""):
                raise AgentInfrastructureError("agent service unavailable")

        repo = Repo()
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(AgentInfrastructureError, "unavailable"):
            Orchestrator(repo.config, NullUI(), agent=OfflineAgent()).run()

        state_path = next(
            (repo.root / ".ralph" / "runtime" / "sessions").glob("*/state.json")
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worktree = Path(state["worktree"])
        manifest = Manifest.load(
            worktree / "docs" / "tasks" / "module" / "manifest.json", worktree
        )
        task = manifest.get("TASK-001")
        self.assertEqual(task.status, "ready")
        self.assertEqual(task.attempts("planning"), 0)

    def test_needs_replan_returns_through_planner(self):
        class ReplanningAgent(SuccessfulAgent):
            def __init__(self):
                self.reviews = 0

            def run(self, role, task, root, result_path, log_path, context=""):
                payload = super().run(role, task, root, result_path, log_path, context)
                if role == "reviewer":
                    self.reviews += 1
                    if self.reviews == 1:
                        payload["status"] = "NEEDS_REPLAN"
                        payload["summary"] = "Plan missed an edge case"
                        payload["acceptanceCriteria"][0]["status"] = "FAIL"
                        result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        repo = Repo()
        self.addCleanup(repo.close)
        agent = ReplanningAgent()
        result = Orchestrator(repo.config, NullUI(), agent=agent).run()
        self.assertEqual(result, 0)
        self.assertEqual(agent.reviews, 2)
        manifest = Manifest.load(repo.config.manifest_path, repo.root)
        task = manifest.get("TASK-001")
        self.assertEqual(task.attempts("planning"), 2)
        self.assertEqual(task.attempts("review"), 2)

    def test_invalid_agent_result_exhausts_attempts_without_merge(self):
        class InvalidAgent(SuccessfulAgent):
            def run(self, role, task, root, result_path, log_path, context=""):
                payload = super().run(role, task, root, result_path, log_path, context)
                if role == "planner":
                    payload.pop("status")
                    result_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload

        task = task_payload()
        task["limits"]["planning"] = 2
        repo = Repo(task)
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        result = Orchestrator(repo.config, NullUI(), agent=InvalidAgent()).run()
        self.assertEqual(result, 3)
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        state_path = next((repo.root / ".ralph" / "runtime" / "sessions").glob("*/state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worktree = Path(state["worktree"])
        manifest = Manifest.load(
            worktree / "docs" / "tasks" / "module" / "manifest.json", worktree
        )
        self.assertEqual(manifest.get("TASK-001").status, "failed")

    def test_mutating_quality_gate_is_rejected(self):
        task = task_payload()
        task["qualityGates"] = [
            {
                "name": "bad gate",
                "command": [
                    "python3",
                    "-c",
                    "from pathlib import Path; Path('gate-side-effect.txt').write_text('bad')",
                ],
                "timeoutSeconds": 10,
            }
        ]
        repo = Repo(task)
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()
        with self.assertRaisesRegex(ScopeError, "Quality gate"):
            Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()
        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        self.assertFalse((repo.root / "gate-side-effect.txt").exists())

    def test_quality_gate_commit_is_rejected_without_merge(self):
        task = task_payload()
        task["qualityGates"] = [
            {
                "name": "committing gate",
                "command": [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; import subprocess; "
                        "Path('forbidden.txt').write_text('escaped\\n'); "
                        "subprocess.run(['git', 'add', 'forbidden.txt'], check=True); "
                        "subprocess.run(['git', 'commit', '-m', 'gate side effect'], "
                        "check=True, stdout=subprocess.DEVNULL)"
                    ),
                ],
                "timeoutSeconds": 30,
            }
        ]
        repo = Repo(task)
        self.addCleanup(repo.close)
        before = run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip()

        with self.assertRaisesRegex(ScopeError, "changed worktree HEAD"):
            Orchestrator(repo.config, NullUI(), agent=SuccessfulAgent()).run()

        self.assertEqual(run(["git", "rev-parse", "HEAD"], repo.root).stdout.strip(), before)
        self.assertFalse((repo.root / "forbidden.txt").exists())


if __name__ == "__main__":
    unittest.main()
