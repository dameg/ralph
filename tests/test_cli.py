from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ralph_loop.cli import _with_prd_override, parser
from ralph_loop.errors import ConfigError


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
            import json

            config = json.loads(
                (root / ".ralph" / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["agent"]["roles"]["planner"]["model"],
                "gpt-5.6-terra",
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
            self.assertIn("The manifest contains no tasks", status.stdout)

    def test_default_role_models_escalate_after_retries(self):
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        agent = repo.config.agent
        self.assertEqual(
            agent.model_for("planner", 1),
            ("gpt-5.6-terra", "medium", False),
        )
        self.assertEqual(
            agent.model_for("planner", 2),
            ("gpt-5.6-sol", "high", True),
        )
        self.assertEqual(
            agent.model_for("implementer", 2),
            ("gpt-5.6-terra", "medium", False),
        )
        self.assertEqual(
            agent.model_for("implementer", 3),
            ("gpt-5.6-sol", "high", True),
        )
        self.assertEqual(
            agent.model_for("reviewer", 1),
            ("gpt-5.6-terra", "high", False),
        )

    def test_run_prd_override_is_transient_and_requires_a_repo_file(self):
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        prd = repo.root / "docs" / "prds" / "from-cursor.md"
        prd.parent.mkdir(parents=True)
        prd.write_text("# Checkout\n", encoding="utf-8")

        args = parser().parse_args(["run", "--prd", "docs/prds/from-cursor.md"])
        configured = _with_prd_override(repo.config, args.prd)
        self.assertEqual(configured.prd_path, prd.resolve())
        self.assertEqual(repo.config.prd_path, (repo.root / "docs" / "prd.md").resolve())
        with self.assertRaisesRegex(ConfigError, "PRD not found"):
            _with_prd_override(repo.config, "docs/prds/missing.md")


if __name__ == "__main__":
    unittest.main()
