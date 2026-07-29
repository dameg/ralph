from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

from .errors import ConfigError, RuntimeBusyError
from .util import atomic_write_json, read_json


class Journal:
    def __init__(self, runtime: Path) -> None:
        self.runtime = runtime
        self.path = runtime / "journal.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **details: Any) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **details,
        }
        encoded = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @contextmanager
    def lock(self) -> Iterator[None]:
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - Ralph targets POSIX worktrees
            raise ConfigError("Ralph v4 requires POSIX runtime locking") from error
        lock_path = self.runtime / "ralph.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeBusyError("Another Ralph process is using this runtime") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Session:
    def __init__(self, path: Path, data: Dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def create(
        cls,
        runtime: Path,
        task_id: str,
        worktree: Path,
        branch: str,
        base_sha: str,
        base_branch: str,
        prd: str,
        cycle_limit: int,
        initial_status: str,
    ) -> "Session":
        path = runtime / "sessions" / task_id / "state.json"
        session = cls(
            path,
            {
                "version": 2,
                "taskId": task_id,
                "worktree": str(worktree),
                "branch": branch,
                "baseSha": base_sha,
                "baseBranch": base_branch,
                "prd": prd,
                "commitSha": None,
                "merged": False,
                "active": True,
                "sequence": 0,
                "cycleSequence": 0,
                "cycleLimit": cycle_limit,
                "cycleGrants": 0,
                "cyclesUsed": 0,
                "planningCredits": 1,
                "currentCycle": None,
                "pendingReview": None,
                "lastReview": None,
                "roleInvocations": {},
                "technicalFailures": {},
                "manualRetries": {},
                "intervention": None,
                "resumeStatus": None,
                "workflowStatus": initial_status,
                "unconsumedRoleRun": None,
            },
        )
        session.save()
        return session

    @classmethod
    def load(cls, path: Path) -> "Session":
        session = cls(path, read_json(path))
        if session.data.get("version") != 2:
            raise ConfigError(f"Session must use version=2: {path}")
        return session

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    def next_sequence(self) -> int:
        self.data["sequence"] = int(self.data.get("sequence", 0)) + 1
        self.save()
        return self.data["sequence"]

    def next_cycle_id(self) -> str:
        self.data["cycleSequence"] = int(self.data.get("cycleSequence", 0)) + 1
        self.save()
        return f"cycle-{self.data['cycleSequence']:03d}"

    def next_role_invocation(self, role: str) -> int:
        invocations = self.data.setdefault("roleInvocations", {})
        invocations[role] = int(invocations.get(role, 0)) + 1
        self.save()
        return invocations[role]

    def cycle_capacity(self) -> int:
        return int(self.data["cycleLimit"]) + int(self.data.get("cycleGrants", 0))

    def has_cycle_capacity(self) -> bool:
        return int(self.data.get("cyclesUsed", 0)) < self.cycle_capacity()
