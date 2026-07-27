from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .errors import ConfigError
from .util import atomic_write_json, ensure_command, ensure_string_list, read_json


EXECUTABLE_STATUSES = {
    "ready",
    "planning",
    "planned",
    "implementing",
    "needs_changes",
    "needs_replan",
    "in_review",
}
ALL_STATUSES = EXECUTABLE_STATUSES | {"blocked", "failed", "completed"}
STAGES = ("planning", "implementation", "review")


@dataclass
class Task:
    raw: Dict[str, Any]
    workspace: str

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def title(self) -> str:
        return self.raw["title"]

    @property
    def status(self) -> str:
        return self.raw["status"]

    @status.setter
    def status(self, value: str) -> None:
        self.raw["status"] = value

    @property
    def depends_on(self) -> List[str]:
        return list(self.raw.get("dependsOn", []))

    @property
    def task_dir(self) -> str:
        return f"{self.workspace.rstrip('/')}/{self.raw['path'].lstrip('/')}"

    @property
    def allowed_paths(self) -> List[str]:
        return list(self.raw["contract"]["allowedPaths"])

    @property
    def acceptance_ids(self) -> List[str]:
        return [item["id"] for item in self.raw["contract"]["acceptanceCriteria"]]

    @property
    def gates(self) -> List[Dict[str, Any]]:
        return list(self.raw["qualityGates"])

    def attempts(self, stage: str) -> int:
        return int(self.raw.setdefault("attempts", {}).get(stage, 0))

    def limit(self, stage: str) -> int:
        return int(self.raw["limits"][stage])

    def increment(self, stage: str) -> int:
        attempts = self.raw.setdefault("attempts", {})
        attempts[stage] = int(attempts.get(stage, 0)) + 1
        return attempts[stage]

    def decrement(self, stage: str) -> int:
        attempts = self.raw.setdefault("attempts", {})
        attempts[stage] = max(0, int(attempts.get(stage, 0)) - 1)
        return attempts[stage]


class Manifest:
    def __init__(self, path: Path, raw: Dict[str, Any]) -> None:
        self.path = path
        self.raw = raw
        self.workspace = raw.get("taskWorkspace", "")
        # An omitted workspace means task paths are relative to the manifest directory.
        if not self.workspace:
            self.workspace = path.parent.as_posix()
        self.tasks = [Task(item, self.workspace) for item in raw.get("tasks", [])]

    @classmethod
    def load(cls, path: Path, repo_root: Path) -> "Manifest":
        if not path.is_file():
            raise ConfigError(f"Manifest not found: {path}")
        try:
            raw = read_json(path)
        except (OSError, ValueError) as error:
            raise ConfigError(f"Cannot read manifest {path}: {error}") from error
        manifest = cls(path, raw)
        workspace = raw.get("taskWorkspace")
        if workspace is None:
            manifest.workspace = path.parent.resolve().relative_to(repo_root.resolve()).as_posix()
        manifest.tasks = [Task(item, manifest.workspace) for item in raw.get("tasks", [])]
        manifest.validate(repo_root)
        return manifest

    def save(self) -> None:
        atomic_write_json(self.path, self.raw)

    def validate(self, repo_root: Path) -> None:
        if self.raw.get("version") != 3:
            raise ConfigError("Manifest must use version=3")
        if self.raw.get("workflow") != "planner-implementer-reviewer":
            raise ConfigError("Manifest must use workflow=planner-implementer-reviewer")
        raw_tasks = self.raw.get("tasks")
        if not isinstance(raw_tasks, list):
            raise ConfigError("manifest.tasks must be an array")
        if not isinstance(self.workspace, str) or not self.workspace:
            raise ConfigError("taskWorkspace must be a non-empty path")
        workspace_path = (repo_root / self.workspace).resolve()
        try:
            workspace_path.relative_to(repo_root.resolve())
        except ValueError as error:
            raise ConfigError("taskWorkspace must be inside the repository") from error

        ids: Set[str] = set()
        for index, raw in enumerate(raw_tasks):
            label = f"tasks[{index}]"
            if not isinstance(raw, dict):
                raise ConfigError(f"{label} must be an object")
            task_id = raw.get("id")
            if not isinstance(task_id, str) or not task_id:
                raise ConfigError(f"{label}.id must be a non-empty string")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id):
                raise ConfigError(
                    f"{label}.id may contain only letters, digits, dots, underscores, and hyphens"
                )
            if task_id in ids:
                raise ConfigError(f"Duplicate task identifier: {task_id}")
            ids.add(task_id)
            if not isinstance(raw.get("title"), str) or not raw["title"]:
                raise ConfigError(f"{task_id}.title must be a non-empty string")
            if raw.get("status") not in ALL_STATUSES:
                raise ConfigError(f"{task_id}.status has an unsupported value")
            if not isinstance(raw.get("path"), str) or not raw["path"]:
                raise ConfigError(f"{task_id}.path must be a non-empty path")
            task_path = (workspace_path / raw["path"]).resolve()
            try:
                task_path.relative_to(workspace_path)
            except ValueError as error:
                raise ConfigError(f"{task_id}.path escapes taskWorkspace") from error
            task_prd = raw.get("prd")
            if task_prd is not None:
                if not isinstance(task_prd, str) or not task_prd:
                    raise ConfigError(f"{task_id}.prd must be a non-empty path")
                prd_path = (repo_root / task_prd).resolve()
                try:
                    prd_path.relative_to(repo_root.resolve())
                except ValueError as error:
                    raise ConfigError(f"{task_id}.prd must be inside the repository") from error
                if not prd_path.is_file():
                    raise ConfigError(f"{task_id}.prd not found: {prd_path}")
            try:
                dependencies = ensure_string_list(raw.get("dependsOn", []), f"{task_id}.dependsOn")
            except ValueError as error:
                raise ConfigError(str(error)) from error
            if task_id in dependencies:
                raise ConfigError(f"{task_id} cannot depend on itself")
            self._validate_contract(raw, task_id)
            self._validate_gates(raw, task_id)
            self._validate_attempts(raw, task_id)

        for task in self.tasks:
            missing = sorted(set(task.depends_on) - ids)
            if missing:
                raise ConfigError(f"{task.id} has unknown dependencies: {', '.join(missing)}")
        self._validate_acyclic()

    def _validate_contract(self, raw: Dict[str, Any], task_id: str) -> None:
        contract = raw.get("contract")
        if not isinstance(contract, dict):
            raise ConfigError(f"{task_id}.contract must be an object")
        criteria = contract.get("acceptanceCriteria")
        if not isinstance(criteria, list) or not criteria:
            raise ConfigError(f"{task_id} must define at least one acceptance criterion")
        criterion_ids: Set[str] = set()
        for criterion in criteria:
            if not isinstance(criterion, dict):
                raise ConfigError(f"{task_id}.contract.acceptanceCriteria contains an invalid value")
            criterion_id = criterion.get("id")
            description = criterion.get("description")
            if not isinstance(criterion_id, str) or not criterion_id.startswith("AC-"):
                raise ConfigError(f"{task_id}: every criterion must use an AC-* identifier")
            if criterion_id in criterion_ids:
                raise ConfigError(f"{task_id}: duplicate criterion {criterion_id}")
            criterion_ids.add(criterion_id)
            if not isinstance(description, str) or not description:
                raise ConfigError(f"{task_id}.{criterion_id} has no description")
        try:
            allowed = ensure_string_list(contract.get("allowedPaths"), f"{task_id}.contract.allowedPaths")
        except ValueError as error:
            raise ConfigError(str(error)) from error
        if not allowed:
            raise ConfigError(f"{task_id}.contract.allowedPaths cannot be empty")
        for pattern in allowed:
            components = pattern.replace("\\", "/").split("/")
            if pattern.startswith(("/", "~")) or ".." in components:
                raise ConfigError(f"{task_id}: forbidden path pattern {pattern!r}")
        non_goals = contract.get("nonGoals", [])
        if not isinstance(non_goals, list) or not all(isinstance(item, str) for item in non_goals):
            raise ConfigError(f"{task_id}.contract.nonGoals must be an array of strings")

    def _validate_gates(self, raw: Dict[str, Any], task_id: str) -> None:
        gates = raw.get("qualityGates")
        if not isinstance(gates, list) or not gates:
            raise ConfigError(f"{task_id}.qualityGates cannot be empty")
        names: Set[str] = set()
        for gate in gates:
            if not isinstance(gate, dict) or not isinstance(gate.get("name"), str) or not gate["name"]:
                raise ConfigError(f"{task_id}: every quality gate must have a name")
            if gate["name"] in names:
                raise ConfigError(f"{task_id}: duplicate quality gate {gate['name']}")
            names.add(gate["name"])
            try:
                gate["command"] = ensure_command(gate.get("command"), f"{task_id}.{gate['name']}.command")
            except ValueError as error:
                raise ConfigError(str(error)) from error
            timeout = gate.get("timeoutSeconds", 600)
            if not isinstance(timeout, int) or timeout < 1:
                raise ConfigError(f"{task_id}.{gate['name']}.timeoutSeconds must be positive")

    def _validate_attempts(self, raw: Dict[str, Any], task_id: str) -> None:
        limits = raw.get("limits")
        attempts = raw.get("attempts", {})
        if not isinstance(limits, dict) or not isinstance(attempts, dict):
            raise ConfigError(f"{task_id}.limits and attempts must be objects")
        for stage in STAGES:
            limit = limits.get(stage)
            attempt = attempts.get(stage, 0)
            if not isinstance(limit, int) or limit < 1:
                raise ConfigError(f"{task_id}.limits.{stage} must be positive")
            if not isinstance(attempt, int) or attempt < 0:
                raise ConfigError(f"{task_id}.attempts.{stage} cannot be negative")

    def _validate_acyclic(self) -> None:
        graph = {task.id: task.depends_on for task in self.tasks}
        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ConfigError(f"Dependency cycle detected at {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in graph[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)

    def get(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ConfigError(f"Task not found: {task_id}")

    def promote_dependencies(self) -> List[str]:
        completed = {task.id for task in self.tasks if task.status == "completed"}
        promoted: List[str] = []
        for task in self.tasks:
            if (
                task.status == "blocked"
                and task.raw.get("blockReason", "dependencies") == "dependencies"
                and set(task.depends_on).issubset(completed)
            ):
                task.status = "ready"
                task.raw.pop("blockReason", None)
                promoted.append(task.id)
        return promoted

    def next_task(self) -> Optional[Task]:
        completed = {task.id for task in self.tasks if task.status == "completed"}
        for task in self.tasks:
            if task.status in EXECUTABLE_STATUSES and set(task.depends_on).issubset(completed):
                return task
        return None

    def counts(self) -> Dict[str, int]:
        return {
            status: sum(1 for task in self.tasks if task.status == status)
            for status in ALL_STATUSES
        }


def blank_manifest(task_workspace: str) -> Dict[str, Any]:
    return {
        "version": 3,
        "workflow": "planner-implementer-reviewer",
        "taskWorkspace": task_workspace,
        "tasks": [],
    }
