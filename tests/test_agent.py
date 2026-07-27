from __future__ import annotations

import unittest

from ralph_loop.agent import CodexAgent, validate_role_result
from ralph_loop.errors import AgentError
from ralph_loop.manifest import Task

from tests.helpers import task_payload


class AgentResultTests(unittest.TestCase):
    def setUp(self):
        self.task = Task(task_payload(), "docs/tasks/module")

    def test_reviewer_pass_requires_all_acceptance_evidence(self):
        with self.assertRaisesRegex(AgentError, "missing"):
            validate_role_result(
                "reviewer",
                {
                    "status": "PASS",
                    "summary": "Looks good",
                    "acceptanceCriteria": [],
                    "findings": [],
                },
                self.task,
            )

    def test_reviewer_pass_rejects_open_finding(self):
        with self.assertRaisesRegex(AgentError, "open"):
            validate_role_result(
                "reviewer",
                {
                    "status": "PASS",
                    "summary": "Looks good",
                    "acceptanceCriteria": [
                        {"id": "AC-001", "status": "PASS", "evidence": "test"}
                    ],
                    "findings": [
                        {
                            "id": "REV-001",
                            "severity": "high",
                            "file": "src/value.txt",
                            "description": "Incorrect value",
                            "expectedBehavior": "The value is correct",
                            "status": "open",
                        }
                    ],
                },
                self.task,
            )

    def test_prompt_uses_task_specific_prd(self):
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        self.task.raw["prd"] = "docs/prds/billing.md"
        prompt = CodexAgent(repo.config)._prompt("planner", self.task, "")
        self.assertIn("docs/prds/billing.md, the PRD assigned to this task", prompt)

    def test_planner_result_requires_the_complete_schema(self):
        with self.assertRaisesRegex(AgentError, "filesPlanned"):
            validate_role_result(
                "planner",
                {
                    "status": "READY",
                    "summary": "Ready",
                    "verificationCommands": [
                        list(gate["command"]) for gate in self.task.gates
                    ],
                },
                self.task,
            )

    def test_planner_result_rejects_files_outside_scope(self):
        with self.assertRaisesRegex(AgentError, "outside the task scope"):
            validate_role_result(
                "planner",
                {
                    "status": "READY",
                    "summary": "Ready",
                    "filesPlanned": ["forbidden.txt"],
                    "verificationCommands": [
                        list(gate["command"]) for gate in self.task.gates
                    ],
                },
                self.task,
            )

    def test_planner_result_must_use_exact_quality_gates(self):
        with self.assertRaisesRegex(AgentError, "exactly match"):
            validate_role_result(
                "planner",
                {
                    "status": "READY",
                    "summary": "Ready",
                    "filesPlanned": ["src/value.txt"],
                    "verificationCommands": [["python3", "-m", "unittest"]],
                },
                self.task,
            )


if __name__ == "__main__":
    unittest.main()
