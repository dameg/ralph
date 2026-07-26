from __future__ import annotations

import unittest

from ralph_loop.agent import validate_role_result
from ralph_loop.errors import AgentError
from ralph_loop.manifest import Task

from tests.helpers import task_payload


class AgentResultTests(unittest.TestCase):
    def setUp(self):
        self.task = Task(task_payload(), "docs/tasks/module")

    def test_reviewer_pass_requires_all_acceptance_evidence(self):
        with self.assertRaisesRegex(AgentError, "brak"):
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
        with self.assertRaisesRegex(AgentError, "otwartymi"):
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


if __name__ == "__main__":
    unittest.main()
