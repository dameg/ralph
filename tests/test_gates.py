from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ralph_loop.errors import GateError
from ralph_loop.gates import GateRunner
from ralph_loop.manifest import Task
from ralph_loop.ui import NullUI

from tests.helpers import task_payload


class GateTests(unittest.TestCase):
    def test_failed_gate_has_a_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = task_payload()
            raw["qualityGates"] = [
                {
                    "name": "failure",
                    "command": ["python3", "-c", "raise SystemExit(7)"],
                    "timeoutSeconds": 10,
                }
            ]
            task = Task(raw, "docs/tasks/module")
            logs = root / "logs"
            with self.assertRaisesRegex(GateError, "exit code 7"):
                GateRunner(NullUI()).run_all(task, root, logs)
            self.assertIn("exit_code=7", next(logs.iterdir()).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
