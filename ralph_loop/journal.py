from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .errors import ConfigError, RuntimeBusyError
from .util import atomic_write_json, read_json


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    available: bool = False

    def add(self, value: Mapping[str, Any]) -> None:
        input_tokens = value.get("inputTokens")
        output_tokens = value.get("outputTokens")
        cached = value.get("cachedInputTokens", 0)
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            return
        if not isinstance(cached, int):
            cached = 0
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_input_tokens += cached
        self.available = True


@dataclass
class WorkMetrics:
    active_seconds: float = 0.0
    usage: UsageTotals = field(default_factory=UsageTotals)
    task_seconds: Dict[str, float] = field(default_factory=dict)
    task_usage: Dict[str, UsageTotals] = field(default_factory=dict)
    prd_seconds: Dict[str, float] = field(default_factory=dict)
    prd_usage: Dict[str, UsageTotals] = field(default_factory=dict)

    def add(self, task_id: str, prd: str, elapsed_seconds: float, usage: Optional[Mapping[str, Any]] = None) -> None:
        elapsed = max(0.0, elapsed_seconds)
        self.active_seconds += elapsed
        self.task_seconds[task_id] = self.task_seconds.get(task_id, 0.0) + elapsed
        self.prd_seconds[prd] = self.prd_seconds.get(prd, 0.0) + elapsed
        if usage is not None:
            self.usage.add(usage)
            self.task_usage.setdefault(task_id, UsageTotals()).add(usage)
            self.prd_usage.setdefault(prd, UsageTotals()).add(usage)


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

    def entries(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        entries: List[Dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    def work_metrics(
        self,
        task_prds: Mapping[str, str],
        active_task_id: Optional[str] = None,
    ) -> WorkMetrics:
        metrics = WorkMetrics()
        started: Dict[str, Dict[str, Any]] = {}
        for entry in self.entries():
            task_id = entry.get("taskId")
            if not isinstance(task_id, str) or task_id not in task_prds:
                continue
            prd = task_prds[task_id]
            event = entry.get("event")
            if event == "role_started":
                run_id = entry.get("runId")
                if isinstance(run_id, str):
                    started[run_id] = entry
                continue
            if event in {"role_accepted", "role_rejected"}:
                elapsed = entry.get("elapsedSeconds")
                start = started.pop(str(entry.get("runId")), None)
                if not isinstance(elapsed, (int, float)):
                    elapsed = _elapsed_between(start, entry)
                metrics.add(task_id, prd, float(elapsed or 0), _usage_entry(entry))
                continue
            if event in {"quality_gates_passed", "quality_gates_failed"}:
                elapsed = entry.get("elapsedSeconds")
                if isinstance(elapsed, (int, float)):
                    metrics.add(task_id, prd, float(elapsed))
        if active_task_id:
            for start in started.values():
                if start.get("taskId") == active_task_id:
                    metrics.add(active_task_id, task_prds[active_task_id], _elapsed_to_now(start))
        return metrics

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


def _usage_entry(entry: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    usage = entry.get("usage")
    return usage if isinstance(usage, dict) else None


def _elapsed_between(start: Optional[Mapping[str, Any]], end: Mapping[str, Any]) -> float:
    if start is None:
        return 0.0
    try:
        return max(0.0, (_parse_timestamp(end["timestamp"]) - _parse_timestamp(start["timestamp"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def _elapsed_to_now(start: Mapping[str, Any]) -> float:
    try:
        return max(0.0, (datetime.now(timezone.utc) - _parse_timestamp(start["timestamp"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
