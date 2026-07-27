from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

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
    ) -> "Session":
        path = runtime / "sessions" / task_id / "state.json"
        session = cls(
            path,
            {
                "version": 1,
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
            },
        )
        session.save()
        return session

    @classmethod
    def load(cls, path: Path) -> "Session":
        return cls(path, read_json(path))

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    def next_sequence(self) -> int:
        self.data["sequence"] = int(self.data.get("sequence", 0)) + 1
        self.save()
        return self.data["sequence"]
