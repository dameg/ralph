from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ralph_loop.config import Config
from ralph_loop.util import atomic_write_json


def run(command: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def task_payload(**overrides: Any) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "id": "TASK-001",
        "title": "Create a value file",
        "status": "ready",
        "path": "TASK-001-create-value",
        "dependsOn": [],
        "contract": {
            "acceptanceCriteria": [{"id": "AC-001", "description": "Value exists"}],
            "allowedPaths": ["src/**", "tests/**"],
            "nonGoals": [],
        },
        "qualityGates": [
            {
                "name": "value check",
                "command": [
                    "python3",
                    "-c",
                    "from pathlib import Path; assert Path('src/value.txt').read_text() == 'ok\\n'",
                ],
                "timeoutSeconds": 30,
            }
        ],
        "limits": {"cycles": 3},
    }
    task.update(overrides)
    return task


class Repo:
    def __init__(self, task: Optional[Dict[str, Any]] = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        run(["git", "init", "-b", "main"], self.root)
        run(["git", "config", "user.name", "Ralph Test"], self.root)
        run(["git", "config", "user.email", "ralph@example.test"], self.root)
        (self.root / ".ralph").mkdir()
        atomic_write_json(
            self.root / ".ralph" / "config.json",
            {
                "version": 1,
                "manifest": "docs/tasks/module/manifest.json",
                "prd": "docs/prd.md",
                "runtime": ".ralph/runtime",
                "maxCyclesPerRun": 10,
                "retryPolicy": {"technicalRetries": 3},
                "agent": {
                    "command": ["codex", "exec"],
                    "sandbox": "workspace-write",
                    "networkAccess": False,
                    "timeoutSeconds": 30,
                },
                "git": {"branchPrefix": "ralph/", "keepBranches": False},
                "ui": {"color": "never"},
            },
        )
        (self.root / ".ralph" / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        (self.root / "docs" / "tasks" / "module" / "TASK-001-create-value").mkdir(
            parents=True
        )
        (self.root / "docs" / "prd.md").write_text("# PRD\n", encoding="utf-8")
        (self.root / "docs" / "tasks" / "module" / "TASK-001-create-value" / "task.md").write_text(
            "# TASK-001\n\n- AC-001\n", encoding="utf-8"
        )
        atomic_write_json(
            self.root / "docs" / "tasks" / "module" / "manifest.json",
            {
                "version": 1,
                "taskWorkspace": "docs/tasks/module",
                "tasks": [task or task_payload()],
            },
        )
        run(["git", "add", "-A"], self.root)
        run(["git", "commit", "-m", "baseline"], self.root)

    @property
    def config(self) -> Config:
        return Config.load(self.root)

    def close(self) -> None:
        self.temp.cleanup()


class SuccessfulAgent:
    def run(self, role, task, root, result_path, log_path, context="", invocation=1):
        criteria = [{"id": "AC-001", "status": "PASS", "evidence": "verified"}]
        if role == "planner":
            (root / task.task_dir / "plan.md").write_text("# Plan\n\nREADY\n", encoding="utf-8")
            payload = {
                "status": "READY",
                "summary": "Plan ready",
                "filesPlanned": ["src/value.txt"],
                "verificationCommands": [
                    list(gate["command"]) for gate in task.gates
                ],
            }
        elif role == "implementer":
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "value.txt").write_text("ok\n", encoding="utf-8")
            (root / task.task_dir / "progress.md").write_text(
                "# Progress\n\nIMPLEMENTATION_COMPLETE\n", encoding="utf-8"
            )
            payload = {
                "status": "IMPLEMENTATION_COMPLETE",
                "summary": "Implemented",
                "changedFiles": ["src/value.txt"],
                "verification": [],
                "acceptanceCriteria": criteria,
            }
        else:
            (root / task.task_dir / "review.md").write_text("# Review\n\nPASS\n", encoding="utf-8")
            payload = {
                "status": "PASS",
                "summary": "Accepted",
                "acceptanceCriteria": criteria,
                "findings": [],
            }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        log_path.write_text("fake agent\n", encoding="utf-8")
        return payload
