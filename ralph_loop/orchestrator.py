from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import __version__
from .agent import Agent, AgentResult, CodexAgent, validate_role_result
from .config import Config
from .errors import (
    AgentContractError,
    AgentError,
    ConfigError,
    GateFailure,
    GateInfrastructureError,
    GitError,
    RalphInternalError,
    ScopeError,
)
from .gates import GateRunner
from .git import Git
from .journal import Journal, Session
from .manifest import EXECUTABLE_STATUSES, Manifest, Task
from .ui import UI
from .util import relative_path, slug


@dataclass(frozen=True)
class RoleRun:
    result: Dict[str, Any]
    run_id: str
    sequence: int


class Orchestrator:
    def __init__(
        self,
        config: Config,
        ui: UI,
        agent: Optional[Agent] = None,
        gates: Optional[GateRunner] = None,
    ) -> None:
        self.config = config
        self.ui = ui
        self.git = Git(config.root)
        self.agent = agent or CodexAgent(config)
        self.gates = gates or GateRunner(ui)
        self.journal = Journal(config.runtime_path)
        self.manifest_relative = relative_path(config.root, config.manifest_path)
        self.runtime_relative = relative_path(config.root, config.runtime_path)

    def run(self, max_cycles: Optional[int] = None) -> int:
        maximum = max_cycles or self.config.max_cycles_per_run
        if maximum < 1:
            raise ConfigError("max cycles must be positive")
        with self.journal.lock():
            return self._run_locked(maximum)

    def _run_locked(self, maximum: int) -> int:
        self._preflight()
        self.ui.banner(
            f"RALPH LOOP {__version__}", "planner → [implementer → gates → reviewer]"
        )
        self.journal.record("run_started", maxCycles=maximum)
        cycles_started = 0
        display_iteration = 0
        seen_prds: Set[str] = set()
        last_task_id: Optional[str] = None

        while True:
            session, manifest, task = self._current_work()
            if task is None:
                return self._finish_without_task(manifest)
            assert manifest is not None
            if session is None:
                if cycles_started >= maximum:
                    return self._pause_at_cycle_limit(manifest, maximum)
                session = self._start_session(task)
                manifest = self._manifest_for_session(session)
                manifest.promote_dependencies()
                manifest.save()
                task = manifest.get(task.id)

            if task.status not in EXECUTABLE_STATUSES:
                return self._finish_without_task(manifest, session)

            if cycles_started >= maximum and session.data.get("currentCycle") is None:
                return self._pause_at_cycle_limit(manifest, maximum, session)

            prd = self._task_prd(task)
            prd_position = self._prd_position(manifest, task, prd)
            if task.id != last_task_id:
                self.ui.prd_started(prd, *prd_position, resumed=prd in seen_prds)
                seen_prds.add(prd)
                last_task_id = task.id
            display_iteration += 1
            self.ui.iteration(
                display_iteration, task.id, task.title, prd=prd, prd_position=prd_position
            )
            self.ui.detail(
                f"State: {task.status} · cycles: "
                f"{session.data['cyclesUsed']}/{session.cycle_capacity()}"
            )
            try:
                started = self._process_stage(session, manifest, task)
            except ScopeError as error:
                self._intervene(
                    session,
                    manifest,
                    task,
                    reason="scope_violation",
                    stage=self._stage_for_status(task.status),
                    resume_status=task.status,
                    details={"error": str(error)},
                )
                return self._finish_without_task(manifest, session)
            except Exception as error:
                self._intervene(
                    session,
                    manifest,
                    task,
                    reason="ralph_internal_error",
                    stage=self._stage_for_status(task.status),
                    resume_status=task.status,
                    details={"error": str(error)},
                )
                raise RalphInternalError(
                    f"Ralph stopped {task.id} after an internal error: {error}"
                ) from error
            cycles_started += int(started)

            if task.status == "completed":
                self._complete(session, manifest, task)

    def retry(
        self,
        task_id: str,
        stage: str,
        attempts: int,
        note: Optional[str] = None,
    ) -> int:
        if attempts < 1:
            raise ConfigError("--attempts must be positive")
        if note is not None and not note.strip():
            raise ConfigError("--note cannot be empty")
        with self.journal.lock():
            self._preflight()
            session, manifest, task = self._active_command_task(task_id)
            intervention = session.data.get("intervention") or {}
            if task.status != "needs_intervention":
                raise ConfigError(f"{task.id} is not waiting for intervention")
            expected_stage = intervention.get("stage")
            if stage != expected_stage:
                raise ConfigError(
                    f"{task.id} requires stage {expected_stage}; received {stage}"
                )
            reason = intervention.get("reason")
            if reason == "cycle_budget_exhausted":
                raise ConfigError(f"Use `ralph extend {task.id} --cycles 1`")
            if reason == "candidate_changed":
                pending = (
                    session.data.get("pendingReview")
                    or session.data.get("currentCycle")
                    or {}
                )
                actual = self._candidate_digest(session, task)
                if actual != pending.get("candidateDigest"):
                    raise ConfigError("The candidate still differs from the pending review")
            if reason == "scope_violation":
                self._require_safe_worktree(session, task)
            if reason == "no_progress":
                if stage != "implementation":
                    raise ConfigError("no_progress can only resume at implementation")
                if not session.has_cycle_capacity():
                    raise ConfigError(f"Use `ralph extend {task.id} --cycles 1` first")
                session.data["stallOverrideOnce"] = True

            retries = session.data.setdefault("manualRetries", {})
            retries[stage] = int(retries.get(stage, 0)) + attempts
            if note:
                session.data["recoveryContext"] = note.strip()
            self._clear_intervention(session)
            self._set_status(
                session,
                manifest,
                task,
                intervention.get("resumeStatus") or self._status_for_stage(stage),
            )
            self.journal.record(
                "manual_retry_granted",
                taskId=task.id,
                stage=stage,
                attempts=attempts,
                noteProvided=bool(note),
            )
            self.ui.success(f"Granted {attempts} additional {stage} invocation(s) for {task.id}")
            return 0

    def extend(self, task_id: str, cycles: int) -> int:
        if cycles < 1:
            raise ConfigError("--cycles must be positive")
        with self.journal.lock():
            self._preflight()
            session, manifest, task = self._active_command_task(task_id)
            intervention = session.data.get("intervention") or {}
            if task.status != "needs_intervention":
                raise ConfigError(f"{task.id} is not waiting for intervention")
            if intervention.get("reason") not in {"cycle_budget_exhausted", "no_progress"}:
                raise ConfigError("The current intervention does not require more cycles")
            session.data["cycleGrants"] = int(session.data.get("cycleGrants", 0)) + cycles
            if intervention.get("reason") == "no_progress":
                session.data["stallOverrideOnce"] = True
            self._clear_intervention(session)
            self._set_status(
                session,
                manifest,
                task,
                intervention.get("resumeStatus") or "needs_changes",
            )
            self.journal.record("cycle_budget_extended", taskId=task.id, cycles=cycles)
            self.ui.success(f"Extended {task.id} by {cycles} cycle(s)")
            return 0

    def _preflight(self) -> None:
        self.git.ensure_repository()
        self.git.ensure_head()
        self.git.ensure_identity()
        self.git.ensure_branch_name(f"{self.config.git.branch_prefix}task")
        if not self.config.prd_path.is_file():
            raise ConfigError(f"PRD not found: {self.config.prd_path}")
        self.git.exclude_runtime(self.runtime_relative)
        active = self._active_sessions()
        if not active:
            self.git.ensure_clean()
            Manifest.load(self.config.manifest_path, self.config.root)
        elif len(active) > 1:
            ids = ", ".join(session.data.get("taskId", "?") for session in active)
            raise GitError(f"More than one active session was detected: {ids}")
        else:
            self._validate_session_prd(active[0])
        if active and self._effective_session_prd(active[0]) != self._session_prd(active[0]):
            raise ConfigError(
                f"Active session {active[0].data['taskId']} is bound to PRD "
                f"{self._effective_session_prd(active[0])}, but its task now declares "
                f"{self._session_prd(active[0])}. Restore the task contract before resuming."
            )
        self._recover_commit(active[0] if active else None)
        if active and active[0].data.get("active"):
            self._reconcile_session(active[0])

    def _active_sessions(self) -> List[Session]:
        sessions: List[Session] = []
        session_root = self.config.runtime_path / "sessions"
        if not session_root.exists():
            return sessions
        for state_path in sorted(session_root.glob("*/state.json")):
            session = Session.load(state_path)
            if session.data.get("active"):
                sessions.append(session)
        return sessions

    def _recover_commit(self, session: Optional[Session]) -> None:
        if session is None:
            return
        commit_sha = session.data.get("commitSha")
        if session.data.get("merged"):
            if not commit_sha or not self._base_branch_contains(session, commit_sha):
                raise GitError(
                    f"Session {session.data['taskId']} is marked as merged, but "
                    f"{session.data['baseBranch']} does not contain its confirmed commit"
                )
            self.journal.record(
                "cleanup_recovered",
                taskId=session.data["taskId"],
                commitSha=commit_sha,
            )
            self._cleanup_session(session)
            return
        worktree = Path(session.data["worktree"])
        if not worktree.is_dir():
            if commit_sha and self._base_branch_contains(session, commit_sha):
                session.data["merged"] = True
                session.save()
                self.journal.record(
                    "merge_recovered",
                    taskId=session.data["taskId"],
                    commitSha=commit_sha,
                )
                self._cleanup_session(session)
                return
            raise GitError(
                f"Worktree for active session {session.data['taskId']} not found: {worktree}"
            )
        worktree_head = self.git.head(worktree)
        manifest = self._manifest_for_session(session)
        task = manifest.get(session.data["taskId"])
        if not commit_sha and worktree_head != session.data["baseSha"] and task.status == "completed":
            self._validate_task_commit(session, task, worktree_head, worktree)
            session.data["commitSha"] = worktree_head
            session.save()
            commit_sha = worktree_head
            self.journal.record("commit_recovered", taskId=task.id, commitSha=worktree_head)
        elif not commit_sha and task.status == "completed":
            if session.data.get("currentCycle"):
                session.data["currentCycle"]["status"] = "finalizing"
            self._set_status(session, manifest, task, "finalizing")
            self.journal.record(
                "precommit_state_recovered", taskId=task.id, nextStatus="finalizing"
            )
        if commit_sha and not session.data.get("merged"):
            self._validate_task_commit(session, task, commit_sha, worktree)
            if self._base_branch_contains(session, commit_sha):
                session.data["merged"] = True
                session.save()
                self.journal.record("merge_recovered", taskId=task.id, commitSha=commit_sha)
                self._cleanup_session(session)
                return
            self.ui.info(f"Recovering the confirmed commit for {task.id}", "♻️ ")
            self._merge_and_cleanup(session)

    def _base_branch_contains(self, session: Session, commit_sha: str) -> bool:
        base_head = self.git.branch_head(session.data["baseBranch"])
        return self.git.is_ancestor(commit_sha, base_head)

    def _validate_session_prd(self, session: Session) -> None:
        value = self._effective_session_prd(session)
        supplied = Path(value)
        if supplied.is_absolute() or value.startswith("~") or ".." in supplied.parts:
            raise ConfigError(f"Session effectivePrd must be repository-relative: {value}")
        worktree = Path(session.data["worktree"])
        root = worktree if worktree.is_dir() else self.config.root
        candidate = (root / supplied).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise ConfigError(f"Session effectivePrd escapes the repository: {value}") from error
        if not candidate.is_file():
            raise ConfigError(f"Session effectivePrd not found: {candidate}")

    def _reconcile_session(self, session: Session) -> None:
        manifest = self._manifest_for_session(session)
        task = manifest.get(session.data["taskId"])
        if session.data.get("intervention"):
            expected = "needs_intervention"
        else:
            expected = session.data.get("workflowStatus")
        if expected and task.status != expected:
            task.status = expected
            manifest.save()
            self.journal.record("state_reconciled", taskId=task.id, status=expected)

    def _set_status(
        self,
        session: Session,
        manifest: Manifest,
        task: Task,
        status: str,
    ) -> None:
        session.data["workflowStatus"] = status
        session.save()
        task.status = status
        manifest.save()

    def _current_work(self) -> Tuple[Optional[Session], Optional[Manifest], Optional[Task]]:
        sessions = self._active_sessions()
        if sessions:
            session = sessions[0]
            manifest = self._manifest_for_session(session)
            return session, manifest, manifest.get(session.data["taskId"])
        manifest = Manifest.load(self.config.manifest_path, self.config.root)
        manifest.promote_dependencies()
        return None, manifest, manifest.next_task()

    def _start_session(self, task: Task) -> Session:
        self.git.ensure_clean()
        base_sha = self.git.head()
        base_branch = self.git.current_branch()
        safe_id = slug(task.id)
        branch = f"{self.config.git.branch_prefix}{safe_id}"
        worktree = self.config.runtime_path / "worktrees" / safe_id
        if worktree.exists():
            raise GitError(f"Target worktree already exists: {worktree}")
        if self.git.branch_exists(branch):
            raise GitError(f"Session branch exists without active state: {branch}")
        with self.ui.step("🌿", f"Creating isolated branch {branch}"):
            self.git.add_worktree(worktree, branch, base_sha)
        session = Session.create(
            self.config.runtime_path,
            task.id,
            worktree,
            branch,
            base_sha,
            base_branch,
            self._task_prd(task),
            task.cycle_limit,
            task.status,
        )
        self.journal.record(
            "session_started",
            taskId=task.id,
            prd=self._task_prd(task),
            branch=branch,
            baseSha=base_sha,
            worktree=str(worktree),
            cycleLimit=task.cycle_limit,
        )
        return session

    def _manifest_for_session(self, session: Session) -> Manifest:
        worktree = Path(session.data["worktree"])
        manifest = Manifest.load(worktree / self.manifest_relative, worktree)
        manifest.get(session.data["taskId"]).session_prd = self._effective_session_prd(
            session
        )
        return manifest

    def _process_stage(self, session: Session, manifest: Manifest, task: Task) -> bool:
        if task.status in {"ready", "planning", "needs_replan"}:
            self._planner(session, manifest, task)
            return False
        if task.status in {"planned", "implementing", "needs_changes"}:
            return self._implementer(session, manifest, task)
        if task.status == "verifying":
            self._verifying(session, manifest, task)
            return False
        if task.status == "in_review":
            self._reviewer(session, manifest, task)
            return False
        if task.status == "finalizing":
            self._finalizing(session, manifest, task)
            return False
        raise ConfigError(f"Cannot process {task.id} from state {task.status}")

    def _planner(self, session: Session, manifest: Manifest, task: Task) -> None:
        if int(session.data.get("planningCredits", 0)) < 1:
            self._intervene(
                session,
                manifest,
                task,
                "missing_planning_credit",
                "planning",
                task.status,
            )
            return
        self._set_status(session, manifest, task, "planning")
        run = self._call_role(
            session, manifest, task, "planner", [f"{task.task_dir}/plan.md"]
        )
        if run is None:
            return
        result = run.result
        if result["status"] == "READY":
            session.data["planningCredits"] = int(session.data["planningCredits"]) - 1
            session.data["unconsumedRoleRun"] = None
            self._set_status(session, manifest, task, "planned")
            self.journal.record("planner_finished", taskId=task.id, status=result["status"])
            self.ui.success("Plan is ready and validated")
            return

        session.data["unconsumedRoleRun"] = None
        reason = "role_blocked" if result["status"] == "BLOCKED" else "needs_clarification"
        self._intervene(
            session,
            manifest,
            task,
            reason,
            "planning",
            "planning",
            {"summary": result["summary"]},
        )
        self.journal.record("planner_finished", taskId=task.id, status=result["status"])

    def _implementer(self, session: Session, manifest: Manifest, task: Task) -> bool:
        started = False
        cycle = session.data.get("currentCycle")
        if cycle is None:
            if not session.has_cycle_capacity():
                self._intervene(
                    session,
                    manifest,
                    task,
                    "cycle_budget_exhausted",
                    "implementation",
                    task.status,
                )
                return False
            cycle = {
                "id": session.next_cycle_id(),
                "status": "implementation",
                "respondsToReviewRunId": (
                    (session.data.get("lastReview") or {}).get("reviewRunId")
                ),
            }
            session.data["currentCycle"] = cycle
            session.save()
            started = True
            self.journal.record("cycle_started", taskId=task.id, cycleId=cycle["id"])

        self._set_status(session, manifest, task, "implementing")
        previous = cycle.get("respondsToReviewRunId")
        context = (
            "Resolve every open REV-* finding from the previous review and preserve IDs. "
            f"This implementation responds to {previous or 'no previous review'}."
        )
        run = self._call_role(
            session,
            manifest,
            task,
            "implementer",
            [*task.allowed_paths, f"{task.task_dir}/progress.md"],
            context,
        )
        if run is None:
            return started
        result = run.result
        if result["status"] == "BLOCKED":
            session.data["currentCycle"] = None
            session.data["unconsumedRoleRun"] = None
            self.journal.record(
                "cycle_released", taskId=task.id, cycleId=cycle["id"], reason="blocked"
            )
            self._intervene(
                session,
                manifest,
                task,
                "role_blocked",
                "implementation",
                "implementing",
                {"summary": result["summary"]},
            )
            return started

        digest = self._candidate_digest(session, task)
        session.data["cyclesUsed"] = int(session.data.get("cyclesUsed", 0)) + 1
        cycle.update(
            {
                "status": "verifying",
                "implementationRunId": run.run_id,
                "implementationStatus": result["status"],
                "candidateDigest": digest,
            }
        )
        pending = {
            "cycleId": cycle["id"],
            "implementationRunId": run.run_id,
            "implementationStatus": result["status"],
            "candidateDigest": digest,
            "gateStatus": None,
            "gateError": None,
        }
        session.data["pendingReview"] = pending
        session.data["unconsumedRoleRun"] = None
        self._set_status(session, manifest, task, "verifying")
        self.journal.record(
            "review_obligation_created",
            taskId=task.id,
            cycleId=cycle["id"],
            implementationRunId=run.run_id,
            candidateDigest=digest,
        )
        self.ui.success("Implementation candidate accepted; independent review is now required")
        return started

    def _verifying(self, session: Session, manifest: Manifest, task: Task) -> None:
        pending = self._pending_review(session, task)
        outcome = self._call_gates(session, manifest, task, "gates")
        if outcome is None:
            return
        status, error = outcome
        pending["gateStatus"] = status
        pending["gateError"] = error
        session.data["currentCycle"]["status"] = "review"
        self._set_status(session, manifest, task, "in_review")

    def _reviewer(self, session: Session, manifest: Manifest, task: Task) -> None:
        pending = self._pending_review(session, task)
        self._require_safe_worktree(session, task)
        actual_digest = self._candidate_digest(session, task)
        if actual_digest != pending["candidateDigest"]:
            self._intervene(
                session,
                manifest,
                task,
                "candidate_changed",
                "review",
                "in_review",
                {"expected": pending["candidateDigest"], "actual": actual_digest},
            )
            return

        def validate_review(result: Dict[str, Any]) -> None:
            previous_open = set((session.data.get("lastReview") or {}).get("openFindingIds", []))
            current_ids = {item["id"] for item in result["findings"]}
            missing = sorted(previous_open - current_ids)
            if missing:
                raise AgentContractError(
                    "Reviewer omitted previous findings: " + ", ".join(missing)
                )
            if result["status"] == "PASS" and (
                pending["implementationStatus"] != "IMPLEMENTATION_COMPLETE"
                or pending["gateStatus"] != "PASS"
            ):
                raise AgentContractError(
                    "Reviewer cannot PASS an incomplete candidate or failed quality gates"
                )

        gate_context = (
            "Quality gates passed."
            if pending["gateStatus"] == "PASS"
            else f"Quality gates failed: {pending['gateError']}. PASS is forbidden."
        )
        run = self._call_role(
            session,
            manifest,
            task,
            "reviewer",
            [f"{task.task_dir}/review.md"],
            (
                f"Review implementation {pending['implementationRunId']} for cycle "
                f"{pending['cycleId']}. {gate_context}"
            ),
            validate_review,
        )
        if run is None:
            return
        result = run.result
        open_ids = sorted(
            item["id"] for item in result["findings"] if item["status"] == "open"
        )
        review_record = {
            "reviewRunId": run.run_id,
            "reviewsImplementationRunId": pending["implementationRunId"],
            "cycleId": pending["cycleId"],
            "candidateDigest": pending["candidateDigest"],
            "openFindingIds": open_ids,
            "status": result["status"],
        }
        previous = session.data.get("lastReview") or {}
        stalled = bool(open_ids) and (
            previous.get("candidateDigest") == pending["candidateDigest"]
            and previous.get("openFindingIds") == open_ids
        )
        if session.data.pop("stallOverrideOnce", False):
            stalled = False
        session.data["lastReview"] = review_record
        session.data["pendingReview"] = None
        session.data["unconsumedRoleRun"] = None
        session.data["currentCycle"]["reviewRunId"] = run.run_id
        session.data["currentCycle"]["reviewStatus"] = result["status"]

        def record_obligation() -> None:
            self.journal.record(
                "review_obligation_fulfilled",
                taskId=task.id,
                cycleId=pending["cycleId"],
                implementationRunId=pending["implementationRunId"],
                reviewRunId=run.run_id,
                status=result["status"],
                candidateDigest=pending["candidateDigest"],
                openFindingIds=open_ids,
            )

        if stalled:
            session.data["currentCycle"] = None
            self._intervene(
                session,
                manifest,
                task,
                "no_progress",
                "implementation",
                "needs_changes",
                {"candidateDigest": pending["candidateDigest"], "openFindingIds": open_ids},
            )
            record_obligation()
            return
        if result["status"] == "PASS":
            session.data["currentCycle"]["status"] = "finalizing"
            self._set_status(session, manifest, task, "finalizing")
            record_obligation()
            return

        session.data["currentCycle"] = None
        if result["status"] == "NEEDS_REPLAN":
            session.data["planningCredits"] = int(session.data.get("planningCredits", 0)) + 1
            next_status = "needs_replan"
        else:
            next_status = "needs_changes"
        if session.has_cycle_capacity():
            self._set_status(session, manifest, task, next_status)
        else:
            self._intervene(
                session,
                manifest,
                task,
                "cycle_budget_exhausted",
                "implementation",
                next_status,
            )
        record_obligation()

    def _finalizing(self, session: Session, manifest: Manifest, task: Task) -> None:
        cycle = session.data.get("currentCycle") or {}
        self._require_safe_worktree(session, task)
        expected = cycle.get("candidateDigest")
        actual = self._candidate_digest(session, task)
        if expected and actual != expected:
            self._intervene(
                session,
                manifest,
                task,
                "candidate_changed",
                "final-gates",
                "finalizing",
                {"expected": expected, "actual": actual},
            )
            return
        outcome = self._call_gates(session, manifest, task, "final-gates")
        if outcome is None:
            return
        status, error = outcome
        if status == "PASS":
            self._set_status(session, manifest, task, "completed")
            return
        session.data["currentCycle"] = None
        session.save()
        if session.has_cycle_capacity():
            self._set_status(session, manifest, task, "needs_changes")
        else:
            self._intervene(
                session,
                manifest,
                task,
                "cycle_budget_exhausted",
                "implementation",
                "needs_changes",
                {"finalGateError": error},
            )

    def _call_role(
        self,
        session: Session,
        manifest: Manifest,
        task: Task,
        role: str,
        allowed: List[str],
        context: str = "",
        extra_validator: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[RoleRun]:
        stage = {"planner": "planning", "implementer": "implementation", "reviewer": "review"}[role]
        unconsumed = session.data.get("unconsumedRoleRun")
        if isinstance(unconsumed, dict) and unconsumed.get("role") == role:
            result = unconsumed["result"]
            if extra_validator:
                extra_validator(result)
            self.journal.record(
                "role_result_recovered",
                taskId=task.id,
                role=role,
                runId=unconsumed["runId"],
            )
            return RoleRun(result, unconsumed["runId"], int(unconsumed["sequence"]))
        recovery_context = session.data.get("recoveryContext")
        if isinstance(recovery_context, str) and recovery_context:
            context = (
                f"{context}\n\nOperator recovery note:\n{recovery_context}"
                if context
                else f"Operator recovery note:\n{recovery_context}"
            )
        while True:
            try:
                run = self._run_role_once(
                    session, task, role, allowed, context, extra_validator
                )
            except AgentError as error:
                if not self._technical_failure(
                    session, manifest, task, stage, str(error), task.status
                ):
                    return None
                continue
            self._reset_technical(session, stage)
            if session.data.pop("recoveryContext", None) is not None:
                session.save()
            return run

    def _run_role_once(
        self,
        session: Session,
        task: Task,
        role: str,
        allowed: List[str],
        context: str,
        extra_validator: Optional[Callable[[Dict[str, Any]], None]],
    ) -> RoleRun:
        worktree = Path(session.data["worktree"])
        sequence = session.next_sequence()
        invocation = session.next_role_invocation(role)
        run_id = f"{role}-{sequence:03d}"
        result_dir = session.path.parent / "results"
        result_path = result_dir / f"{sequence:03d}-{role}.json"
        log_path = result_dir / f"{sequence:03d}-{role}.log"
        model, effort, escalated = self.config.agent.model_for(role, invocation)
        prd = self._task_prd(task)
        self.ui.detail(
            f"Model: {model} · reasoning: {effort}" + (" · escalated" if escalated else "")
        )
        self.journal.record(
            "role_started",
            taskId=task.id,
            prd=prd,
            role=role,
            runId=run_id,
            invocation=invocation,
            model=model,
            reasoningEffort=effort,
            escalated=escalated,
        )
        before_head = self.git.head(worktree)
        if before_head != session.data["baseSha"]:
            raise ScopeError(
                f"Role {role} started with worktree HEAD {before_head}; "
                f"expected {session.data['baseSha']}"
            )
        before = self.git.file_snapshot(worktree)
        caught: Optional[Exception] = None
        result: Optional[Dict[str, Any]] = None
        usage = None
        started = time.monotonic()
        try:
            with self.ui.step(
                {"planner": "🧠", "implementer": "🛠️ ", "reviewer": "🔍"}[role],
                f"{role.title()} · {task.id}",
            ):
                agent_result = self.agent.run(
                    role,
                    task,
                    worktree,
                    result_path,
                    log_path,
                    context,
                    invocation,
                )
            if isinstance(agent_result, AgentResult):
                result = agent_result.payload
                usage = agent_result.usage
            else:
                result = agent_result
            validate_role_result(role, result, task)
            self._require_artifact(session, task, {
                "planner": "plan.md", "implementer": "progress.md", "reviewer": "review.md"
            }[role])
            if extra_validator:
                extra_validator(result)
        except Exception as error:
            caught = error
        after = self.git.file_snapshot(worktree)
        after_head = self.git.head(worktree)
        if after_head != before_head:
            raise ScopeError(
                f"Role {role} changed worktree HEAD from {before_head} to {after_head}"
            )
        changed = self.git.changed_since(before, after)
        self._assert_role_scope(role, task, changed, allowed)
        if caught:
            self.journal.record(
                "role_rejected",
                taskId=task.id,
                prd=prd,
                role=role,
                runId=run_id,
                error=str(caught),
                elapsedSeconds=round(time.monotonic() - started, 3),
            )
            if isinstance(caught, AgentError):
                raise caught
            raise AgentContractError(f"Role {role} failed: {caught}") from caught
        assert result is not None
        session.data["unconsumedRoleRun"] = {
            "role": role,
            "runId": run_id,
            "sequence": sequence,
            "result": result,
        }
        session.save()
        self.journal.record(
            "role_accepted",
            taskId=task.id,
            prd=prd,
            role=role,
            runId=run_id,
            invocation=invocation,
            changedPaths=sorted(changed),
            resultPath=str(result_path),
            logPath=str(log_path),
            status=result["status"],
            elapsedSeconds=round(time.monotonic() - started, 3),
            usage=usage.as_dict() if usage else None,
        )
        return RoleRun(result, run_id, sequence)

    def _assert_role_scope(
        self, role: str, task: Task, changed: set[str], allowed: List[str]
    ) -> None:
        artifact = {"planner": "plan.md", "implementer": "progress.md", "reviewer": "review.md"}[role]
        role_artifact = f"{task.task_dir}/{artifact}".lstrip("./")
        workflow_names = {"task.md", "plan.md", "progress.md", "review.md"}
        workspace_prefix = task.workspace.strip("/")
        if workspace_prefix == ".":
            workspace_prefix = ""
        violations = sorted(
            path for path in changed
            if (not workspace_prefix or path.startswith(workspace_prefix + "/"))
            and Path(path).name in workflow_names and path != role_artifact
        )
        if violations:
            raise ScopeError(
                f"Role {role} modified artifacts belonging to another stage or task: "
                + ", ".join(violations)
            )
        protected = self._protected_paths(task)
        if role != "planner":
            protected.append(f"{task.task_dir}/plan.md")
        if role != "reviewer":
            protected.append(f"{task.task_dir}/review.md")
        if role != "implementer":
            protected.append(f"{task.task_dir}/progress.md")
        self.git.assert_scope(changed, allowed, role, protected)

    def _call_gates(
        self,
        session: Session,
        manifest: Manifest,
        task: Task,
        stage: str,
    ) -> Optional[Tuple[str, Optional[str]]]:
        while True:
            try:
                self._run_gates_once(session, task, stage)
            except GateFailure as error:
                self._reset_technical(session, stage)
                return "FAIL", str(error)
            except GateInfrastructureError as error:
                if not self._technical_failure(
                    session, manifest, task, stage, str(error), task.status
                ):
                    return None
                continue
            self._reset_technical(session, stage)
            return "PASS", None

    def _run_gates_once(self, session: Session, task: Task, stage: str) -> None:
        worktree = Path(session.data["worktree"])
        before_head = self.git.head(worktree)
        if before_head != session.data["baseSha"]:
            raise ScopeError(
                f"Quality gate started with worktree HEAD {before_head}; "
                f"expected {session.data['baseSha']}"
            )
        sequence = session.next_sequence()
        log_dir = session.path.parent / "gates" / f"{sequence:03d}-{stage}"
        before = self.git.file_snapshot(worktree)
        caught: Optional[Exception] = None
        results = []
        prd = self._task_prd(task)
        started = time.monotonic()
        self.journal.record(
            "quality_gates_started", taskId=task.id, prd=prd, stage=stage, sequence=sequence
        )
        try:
            results = self.gates.run_all(task, worktree, log_dir)
        except Exception as error:
            caught = error
        after = self.git.file_snapshot(worktree)
        after_head = self.git.head(worktree)
        if after_head != before_head:
            raise ScopeError(
                f"Quality gate changed worktree HEAD from {before_head} to {after_head}"
            )
        changed = self.git.changed_since(before, after)
        if changed:
            raise ScopeError("Quality gate modified the worktree: " + ", ".join(sorted(changed)))
        if caught:
            self.journal.record(
                "quality_gates_failed",
                taskId=task.id,
                prd=prd,
                stage=stage,
                sequence=sequence,
                error=str(caught),
                elapsedSeconds=round(time.monotonic() - started, 3),
            )
            if isinstance(caught, (GateFailure, GateInfrastructureError)):
                raise caught
            raise GateInfrastructureError(f"Cannot run quality gates: {caught}") from caught
        self.journal.record(
            "quality_gates_passed",
            taskId=task.id,
            prd=prd,
            stage=stage,
            sequence=sequence,
            elapsedSeconds=round(time.monotonic() - started, 3),
            results=[
                {
                    "name": item.name,
                    "command": item.command,
                    "exitCode": item.exit_code,
                    "elapsedSeconds": round(item.elapsed_seconds, 3),
                    "logPath": str(item.log_path),
                }
                for item in results
            ],
        )

    def _technical_failure(
        self,
        session: Session,
        manifest: Manifest,
        task: Task,
        stage: str,
        error: str,
        resume_status: str,
    ) -> bool:
        failures = session.data.setdefault("technicalFailures", {})
        failures[stage] = int(failures.get(stage, 0)) + 1
        manual = int(session.data.setdefault("manualRetries", {}).get(stage, 0))
        allowed_attempts = 1 + self.config.technical_retries + manual
        session.save()
        if failures[stage] >= allowed_attempts:
            self._intervene(
                session,
                manifest,
                task,
                "technical_retries_exhausted",
                stage,
                resume_status,
                {"error": error, "attempts": failures[stage]},
            )
            return False
        self.journal.record(
            "technical_retry",
            taskId=task.id,
            stage=stage,
            failure=failures[stage],
            maxAttempts=allowed_attempts,
            error=error,
        )
        self.ui.warning(error)
        return True

    def _reset_technical(self, session: Session, stage: str) -> None:
        session.data.setdefault("technicalFailures", {})[stage] = 0
        session.data.setdefault("manualRetries", {})[stage] = 0
        session.save()

    def _intervene(
        self,
        session: Session,
        manifest: Manifest,
        task: Task,
        reason: str,
        stage: str,
        resume_status: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if reason == "cycle_budget_exhausted" or (
            reason == "no_progress" and not session.has_cycle_capacity()
        ):
            actions = [["ralph", "extend", task.id, "--cycles", "1"]]
        else:
            actions = [["ralph", "retry", task.id, "--stage", stage, "--attempts", "1"]]
        intervention = {
            "reason": reason,
            "stage": stage,
            "resumeStatus": resume_status,
            "details": details or {},
            "nextActions": actions,
        }
        session.data["intervention"] = intervention
        self._set_status(session, manifest, task, "needs_intervention")
        self.journal.record(
            "intervention_required", taskId=task.id, **intervention
        )
        self.ui.warning(f"{task.id} needs intervention: {reason}")

    def _clear_intervention(self, session: Session) -> None:
        session.data["intervention"] = None

    def _pending_review(self, session: Session, task: Task) -> Dict[str, Any]:
        pending = session.data.get("pendingReview")
        if not isinstance(pending, dict):
            raise ConfigError(f"{task.id} has no pending review obligation")
        return pending

    def _candidate_digest(self, session: Session, task: Task) -> str:
        return self.git.candidate_digest(Path(session.data["worktree"]), task.allowed_paths)

    def _require_artifact(self, session: Session, task: Task, name: str) -> None:
        path = Path(session.data["worktree"]) / task.task_dir / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise AgentContractError(f"Role did not create the required file {path}")

    def _require_safe_worktree(self, session: Session, task: Task) -> None:
        worktree = Path(session.data["worktree"])
        actual_head = self.git.head(worktree)
        if actual_head != session.data["baseSha"]:
            raise ConfigError(
                f"Task branch HEAD is {actual_head}; restore it to {session.data['baseSha']} "
                "before retrying"
            )
        administrative = self._administrative_paths(task)
        self.git.assert_scope(
            self.git.changed_paths(worktree),
            [*task.allowed_paths, *administrative],
            "retry",
            self._protected_paths(task),
        )

    def _active_command_task(self, task_id: str) -> Tuple[Session, Manifest, Task]:
        sessions = self._active_sessions()
        if len(sessions) != 1:
            raise ConfigError("Exactly one active session is required")
        session = sessions[0]
        if session.data["taskId"] != task_id:
            raise ConfigError(f"Active task is {session.data['taskId']}, not {task_id}")
        manifest = self._manifest_for_session(session)
        return session, manifest, manifest.get(task_id)

    @staticmethod
    def _stage_for_status(status: str) -> str:
        return {
            "planning": "planning",
            "implementing": "implementation",
            "verifying": "gates",
            "in_review": "review",
            "finalizing": "final-gates",
        }.get(status, "implementation")

    @staticmethod
    def _status_for_stage(stage: str) -> str:
        return {
            "planning": "planning",
            "implementation": "implementing",
            "gates": "verifying",
            "review": "in_review",
            "final-gates": "finalizing",
        }[stage]

    def _complete(self, session: Session, manifest: Manifest, task: Task) -> None:
        worktree = Path(session.data["worktree"])
        branch_head = self.git.head(worktree)
        if branch_head != session.data["baseSha"]:
            raise GitError(
                f"Task branch HEAD is {branch_head}; expected unchanged base "
                f"{session.data['baseSha']} before finalization"
            )
        manifest.promote_dependencies()
        manifest.save()
        administrative = self._administrative_paths(task)
        changed = self.git.changed_paths(worktree)
        self.git.assert_scope(
            changed,
            [*task.allowed_paths, *administrative],
            "finalizer",
            self._protected_paths(task),
        )
        with self.ui.step("📦", "Creating an atomic task commit"):
            commit_sha = self.git.commit_all(worktree, f"{task.id}: {task.title}")
            self._validate_task_commit(session, task, commit_sha, worktree)
            session.data["commitSha"] = commit_sha
            session.save()
            self.journal.record("commit_confirmed", taskId=task.id, commitSha=commit_sha)
        self._merge_and_cleanup(session)
        self.ui.success(f"{task.id} completed · {commit_sha[:8]}")
        if manifest.counts().get("completed", 0) < len(manifest.tasks):
            self._show_summary(manifest, task)

    def _merge_and_cleanup(self, session: Session) -> None:
        commit_sha = session.data.get("commitSha")
        if not commit_sha:
            raise GitError("Cannot merge a session without a confirmed commit")
        worktree = Path(session.data["worktree"])
        if worktree.is_dir():
            manifest = self._manifest_for_session(session)
            self._validate_task_commit(
                session, manifest.get(session.data["taskId"]), commit_sha, worktree
            )
        with self.ui.step("🔗", f"Fast-forwarding {session.data['baseBranch']}"):
            merged_sha = self.git.fast_forward(
                session.data["baseSha"], session.data["branch"], session.data["baseBranch"]
            )
            if merged_sha != commit_sha:
                raise GitError(f"HEAD after merge is {merged_sha}; expected {commit_sha}")
            session.data["merged"] = True
            session.save()
            self.journal.record(
                "merge_confirmed", taskId=session.data["taskId"], commitSha=commit_sha
            )
        self._cleanup_session(session)

    def _validate_task_commit(
        self, session: Session, task: Task, commit_sha: str, worktree: Path
    ) -> None:
        commit_parent = self.git.commit_parent(commit_sha, worktree)
        if commit_parent != session.data["baseSha"]:
            raise GitError(
                f"Final commit parent is {commit_parent}; expected {session.data['baseSha']}"
            )
        self.git.assert_scope(
            self.git.committed_paths(session.data["baseSha"], commit_sha, worktree),
            [*task.allowed_paths, *self._administrative_paths(task)],
            "final commit",
            self._protected_paths(task),
        )

    def _administrative_paths(self, task: Task) -> List[str]:
        return [
            self.manifest_relative,
            f"{task.task_dir}/plan.md",
            f"{task.task_dir}/progress.md",
            f"{task.task_dir}/review.md",
        ]

    def _protected_paths(self, task: Task) -> List[str]:
        configured_prd = relative_path(self.config.root, self.config.prd_path)
        return [
            ".git/**",
            ".ralph/**",
            "references/**",
            "AGENTS.md",
            "**/AGENTS.md",
            configured_prd,
            self._task_prd(task),
            f"{task.task_dir}/task.md",
        ]

    def _cleanup_session(self, session: Session) -> None:
        worktree = Path(session.data["worktree"])
        if worktree.exists():
            self.git.remove_worktree(worktree)
        if not self.config.git.keep_branches and self.git.branch_exists(session.data["branch"]):
            self.git.delete_branch(session.data["branch"])
        session.data["active"] = False
        session.save()
        self.journal.record("session_completed", taskId=session.data["taskId"])

    def _pause_at_cycle_limit(
        self,
        manifest: Manifest,
        maximum: int,
        session: Optional[Session] = None,
    ) -> int:
        self.journal.record("run_cycle_limit_reached", maxCycles=maximum)
        self.ui.warning(f"Reached the {maximum}-cycle run limit; state can be resumed safely.")
        self._show_summary(manifest, active_task_id=session.data["taskId"] if session else None)
        return 2

    def _finish_without_task(
        self, manifest: Optional[Manifest], session: Optional[Session] = None
    ) -> int:
        if manifest is None:
            raise ConfigError("Cannot read task state")
        self._show_summary(manifest, active_task_id=session.data["taskId"] if session else None)
        counts = manifest.counts()
        total = len(manifest.tasks)
        completed = counts.get("completed", 0)
        if total == 0:
            self.ui.warning("The manifest does not contain any tasks yet.")
            return 0
        if completed == total:
            self.journal.record("run_completed", completed=completed, total=total)
            self.ui.success("All tasks are complete 🎉")
            return 0
        if session:
            task = manifest.get(session.data["taskId"])
            self.ui.warning(
                f"{task.id} stopped in state {task.status}; branch: {session.data['branch']}"
            )
            self.ui.info(f"Worktree for inspection: {session.data['worktree']}", "📁")
            intervention = session.data.get("intervention")
            if intervention:
                for action in intervention.get("nextActions", []):
                    self.ui.info("Next action: " + " ".join(action), "➡️ ")
        else:
            self.ui.warning("No executable tasks remain; check dependencies and blockers.")
        return 3

    def _show_summary(
        self,
        manifest: Manifest,
        completed_task: Optional[Task] = None,
        active_task_id: Optional[str] = None,
    ) -> None:
        counts = manifest.counts()
        task_prds = {task.id: self._task_prd(task) for task in manifest.tasks}
        metrics = self.journal.work_metrics(task_prds, active_task_id)
        all_complete = bool(manifest.tasks) and counts.get("completed", 0) == len(manifest.tasks)
        self.ui.summary(
            counts.get("completed", 0),
            len(manifest.tasks),
            counts.get("blocked", 0),
            counts.get("needs_intervention", 0),
            active_seconds=metrics.active_seconds,
            task_usage=(metrics.task_usage.get(completed_task.id) if completed_task else None),
            total_usage=metrics.usage,
            prd_rows=self._prd_rows(manifest, metrics) if all_complete else None,
            all_complete=all_complete,
        )

    def _task_prd(self, task: Task) -> str:
        configured = relative_path(self.config.root, self.config.prd_path)
        return task.effective_prd(configured)

    def _session_prd(self, session: Session) -> str:
        worktree = Path(session.data["worktree"])
        try:
            root = worktree if worktree.is_dir() else self.config.root
            manifest = Manifest.load(root / self.manifest_relative, root)
            task = manifest.get(session.data["taskId"])
            return str(task.raw.get("prd") or self._effective_session_prd(session))
        except (ConfigError, KeyError, OSError):
            return self._effective_session_prd(session)

    @staticmethod
    def _effective_session_prd(session: Session) -> str:
        value = session.data.get("effectivePrd", "")
        return str(value)

    def _prd_position(self, manifest: Manifest, task: Task, prd: str) -> Tuple[int, int]:
        grouped = [item for item in manifest.tasks if self._task_prd(item) == prd]
        return grouped.index(task) + 1, len(grouped)

    def _prd_rows(self, manifest: Manifest, metrics: Any) -> List[Tuple[str, int, int, float, Any]]:
        prds: List[str] = []
        for task in manifest.tasks:
            prd = self._task_prd(task)
            if prd not in prds:
                prds.append(prd)
        rows = []
        for prd in prds:
            tasks = [task for task in manifest.tasks if self._task_prd(task) == prd]
            rows.append((
                prd,
                sum(task.status == "completed" for task in tasks),
                len(tasks),
                metrics.prd_seconds.get(prd, 0.0),
                metrics.prd_usage.get(prd),
            ))
        return rows
