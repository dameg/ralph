from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_init_and_status_on_fresh_repository(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=str(root), check=True, capture_output=True
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(source)
            environment["PYTHONPYCACHEPREFIX"] = "/tmp/ralph-test-pycache"
            initialized = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ralph_loop",
                    "init",
                    "--prd",
                    "docs/module-prd.md",
                    "--manifest",
                    "docs/tasks/module/manifest.json",
                ],
                cwd=str(root),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertTrue((root / ".ralph" / "prompts" / "planner.md").is_file())
            self.assertTrue(
                (root / ".ralph" / "schemas" / "reviewer-result.schema.json").is_file()
            )
            status = subprocess.run(
                [sys.executable, "-m", "ralph_loop", "status", "--color", "never"],
                cwd=str(root),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("Manifest nie zawiera zadań", status.stdout)


if __name__ == "__main__":
    unittest.main()
