from __future__ import annotations

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
        import json

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
        import json

        path = repo.config.manifest_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["tasks"][0]["prd"] = "docs/second-prd.md"
        path.write_text(json.dumps(data), encoding="utf-8")
        manifest = Manifest.load(path, repo.root)
        self.assertEqual(manifest.get("TASK-001").raw["prd"], "docs/second-prd.md")


if __name__ == "__main__":
    unittest.main()
