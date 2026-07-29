from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from ralph_loop.cli import parser
from ralph_loop.errors import ConfigError


class CliTests(unittest.TestCase):
    def test_reviewer_schema_avoids_unsupported_composition(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "ralph_loop/templates/schemas/reviewer-result.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertNotIn("allOf", schema)

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
            config = json.loads(
                (root / ".ralph" / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["version"], 1)
            self.assertEqual(config["maxCyclesPerRun"], 30)
            self.assertEqual(config["retryPolicy"]["technicalRetries"], 3)
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
            manifest = json.loads(
                (root / "docs/tasks/module/manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], 1)
            self.assertNotIn("workflow", manifest)

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

    def test_operational_commands_reject_prd_override(self):
        commands = [
            ["run", "--prd", "docs/other.md"],
            [
                "retry",
                "TASK-001",
                "--stage",
                "review",
                "--attempts",
                "1",
                "--prd",
                "docs/other.md",
            ],
            ["extend", "TASK-001", "--cycles", "1", "--prd", "docs/other.md"],
        ]
        for command in commands:
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parser().parse_args(command)

    def test_v1_cli_uses_cycle_and_intervention_commands(self):
        run_args = parser().parse_args(["run", "--max-cycles", "7"])
        self.assertEqual(run_args.max_cycles, 7)
        retry = parser().parse_args(
            [
                "retry",
                "TASK-001",
                "--stage",
                "review",
                "--attempts",
                "2",
                "--note",
                "Use the approved API",
            ]
        )
        self.assertEqual((retry.task_id, retry.stage, retry.attempts), ("TASK-001", "review", 2))
        self.assertEqual(retry.note, "Use the approved API")
        extend = parser().parse_args(["extend", "TASK-001", "--cycles", "1"])
        self.assertEqual((extend.task_id, extend.cycles), ("TASK-001", 1))

    def test_config_v2_is_rejected(self):
        from ralph_loop.config import Config
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.root / ".ralph/config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = 2
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "version=1"):
            Config.load(repo.root)

    def test_unknown_config_fields_are_rejected(self):
        from ralph_loop.config import Config
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.root / ".ralph/config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agent"]["timeoutsSeconds"] = 10
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "timeoutsSeconds"):
            Config.load(repo.root)

    def test_boolean_numeric_config_values_are_rejected(self):
        from ralph_loop.config import Config
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.root / ".ralph/config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["maxCyclesPerRun"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "maxCyclesPerRun"):
            Config.load(repo.root)

    def test_config_paths_must_be_repository_relative(self):
        from ralph_loop.config import Config
        from tests.helpers import Repo

        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.root / ".ralph/config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["runtime"] = "~/ralph-runtime"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "relative path"):
            Config.load(repo.root)

    def test_non_v1_sessions_are_rejected(self):
        from ralph_loop.journal import Session

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for version in (2, 3, 1.0):
                with self.subTest(version=version):
                    path.write_text(json.dumps({"version": version}), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, "version=1"):
                        Session.load(path)


if __name__ == "__main__":
    unittest.main()
