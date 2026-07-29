from __future__ import annotations

import json
import unittest

from ralph_loop.errors import ConfigError
from ralph_loop.manifest import Manifest

from tests.helpers import Repo, task_payload


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_loads(self):
        repo = Repo()
        self.addCleanup(repo.close)
        manifest = Manifest.load(repo.config.manifest_path, repo.root)
        self.assertEqual(manifest.tasks[0].acceptance_ids, ["AC-001"])
        self.assertEqual(manifest.tasks[0].allowed_paths, ["src/**", "tests/**"])

    def test_dependency_cycle_is_rejected(self):
        first = task_payload(dependsOn=["TASK-002"])
        second = task_payload(
            id="TASK-002",
            title="Second",
            path="TASK-002-second",
            dependsOn=["TASK-001"],
        )
        repo = Repo(first)
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"].append(second)
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "cycle"):
            Manifest.load(path, repo.root)

    def test_shell_string_gate_is_rejected(self):
        task = task_payload()
        task["qualityGates"][0]["command"] = "npm test && npm run lint"
        repo = Repo(task)
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(ConfigError, "command"):
            Manifest.load(repo.config.manifest_path, repo.root)

    def test_task_can_reference_its_own_prd(self):
        repo = Repo()
        self.addCleanup(repo.close)
        prd = repo.root / "docs" / "second-prd.md"
        prd.write_text("# Second PRD\n", encoding="utf-8")
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["prd"] = "docs/second-prd.md"
        path.write_text(json.dumps(data), encoding="utf-8")
        manifest = Manifest.load(path, repo.root)
        self.assertEqual(manifest.get("TASK-001").raw["prd"], "docs/second-prd.md")

    def test_manifest_v4_is_rejected(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = 4
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "version=1"):
            Manifest.load(path, repo.root)

    def test_workflow_discriminator_is_rejected(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["workflow"] = "planner-implementer-reviewer"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "workflow"):
            Manifest.load(path, repo.root)

    def test_runtime_task_fields_are_rejected(self):
        for field, value in (
            ("blockReason", "dependencies"),
            ("lastFailure", "failed"),
            ("intervention", {}),
        ):
            with self.subTest(field=field):
                repo = Repo()
                self.addCleanup(repo.close)
                path = repo.config.manifest_path
                data = json.loads(path.read_text(encoding="utf-8"))
                data["tasks"][0][field] = value
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, field):
                    Manifest.load(path, repo.root)

    def test_stage_attempts_are_rejected(self):
        task = task_payload()
        task["attempts"] = {"planning": 0, "implementation": 0, "review": 0}
        repo = Repo(task)
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(ConfigError, "attempts is not supported"):
            Manifest.load(repo.config.manifest_path, repo.root)

    def test_cycle_limit_defaults_to_five(self):
        task = task_payload()
        task.pop("limits")
        repo = Repo(task)
        self.addCleanup(repo.close)
        manifest = Manifest.load(repo.config.manifest_path, repo.root)
        self.assertEqual(manifest.get("TASK-001").cycle_limit, 5)

    def test_unknown_manifest_and_nested_fields_are_rejected(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["contract"]["allowedPath"] = ["src/**"]
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "allowedPath"):
            Manifest.load(path, repo.root)

    def test_boolean_cycle_and_gate_timeouts_are_rejected(self):
        task = task_payload()
        task["limits"]["cycles"] = True
        repo = Repo(task)
        self.addCleanup(repo.close)
        with self.assertRaisesRegex(ConfigError, "cycles must be positive"):
            Manifest.load(repo.config.manifest_path, repo.root)

        task["limits"]["cycles"] = 1
        task["qualityGates"][0]["timeoutSeconds"] = True
        repo.config.manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "taskWorkspace": "docs/tasks/module",
                    "tasks": [task],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "timeoutSeconds must be positive"):
            Manifest.load(repo.config.manifest_path, repo.root)

    def test_absolute_and_home_relative_paths_are_rejected(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["taskWorkspace"] = "/tmp/tasks"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "relative path"):
            Manifest.load(path, repo.root)

        data["taskWorkspace"] = "docs/tasks/module"
        data["tasks"][0]["path"] = "~/task"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "relative path"):
            Manifest.load(path, repo.root)

    def test_duplicate_resolved_task_directory_is_rejected(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        duplicate = task_payload(id="TASK-002", title="Duplicate directory")
        data["tasks"].append(duplicate)
        path.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "duplicate task directory"):
            Manifest.load(path, repo.root)


if __name__ == "__main__":
    unittest.main()
