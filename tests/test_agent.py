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
                            "status": "open",
                        }
                    ],
                },
                self.task,
            )

    def test_reviewer_fail_requires_an_open_finding(self):
        with self.assertRaisesRegex(AgentError, "open finding"):
            validate_role_result(
                "reviewer",
                {
                    "status": "FAIL",
                    "summary": "Rejected without actionable feedback",
                    "acceptanceCriteria": [
                        {"id": "AC-001", "status": "FAIL", "evidence": "missing"}
                    ],
                    "findings": [],
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


if __name__ == "__main__":
    unittest.main()
