from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent import Agent, CodexAgent, validate_role_result
from .config import Config
from .errors import AgentError, ConfigError, GateError, GitError, ScopeError
from .gates import GateRunner
from .git import Git
from .journal import Journal, Session
from .manifest import EXECUTABLE_STATUSES, Manifest, Task
from .ui import UI
from .util import relative_path, slug


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

    def run(self, max_iterations: Optional[int] = None) -> int:
        maximum = max_iterations or self.config.max_iterations
        self._preflight()
        self.ui.banner("RALPH LOOP v3", "planner → implementer → reviewer")
        self.journal.record("run_started", maxIterations=maximum)

        for iteration in range(1, maximum + 1):
            session, manifest, task = self._current_work()
            if task is None:
                return self._finish_without_task(manifest)
            assert manifest is not None
            if session is None:
                session = self._start_session(task)
                manifest = self._manifest_for_session(session)
                manifest.promote_dependencies()
                manifest.save()
                task = manifest.get(task.id)

            if task.status not in EXECUTABLE_STATUSES:
                return self._finish_without_task(manifest, session)

            self.ui.iteration(iteration, maximum, task.id, task.title)
            self.ui.detail(
                f"State: {task.status} · P/I/R attempts: "
                f"{task.attempts('planning')}/{task.attempts('implementation')}/{task.attempts('review')}"
            )
            try:
                self._process_stage(session, manifest, task)
            except ScopeError as error:
                task.status = "failed"
                task.raw["blockReason"] = "scope_violation"
                manifest.save()
                self.journal.record(
                    "scope_violation", taskId=task.id, error=str(error), branch=session.data["branch"]
                )
                raise

            if task.status == "completed":
                self._complete(session, manifest, task)

        session, manifest, task = self._current_work()
        if task is None:
            return self._finish_without_task(manifest, session)
        self.journal.record("run_limit_reached", maxIterations=maximum)
        self.ui.warning(f"Reached the {maximum}-iteration limit; the state can be resumed safely.")
        if manifest:
            self._show_summary(manifest)
        return 2

    def _preflight(self) -> None:
        self.git.ensure_repository()
        self.git.ensure_head()
        self.git.ensure_identity()
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
        self._recover_commit(active[0] if active else None)

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
        worktree = Path(session.data["worktree"])
        if not worktree.is_dir():
            if (
                session.data.get("commitSha")
                and self.git.head() == session.data["commitSha"]
            ):
                if (
                    not self.config.git.keep_branches
                    and self.git.branch_exists(session.data["branch"])
                ):
                    self.git.delete_branch(session.data["branch"])
                session.data["merged"] = True
                session.data["active"] = False
                session.save()
                self.journal.record(
                    "cleanup_recovered",
                    taskId=session.data["taskId"],
                    commitSha=session.data["commitSha"],
                )
                return
            raise GitError(
                f"Worktree for active session {session.data['taskId']} not found: {worktree}"
            )
        worktree_head = self.git.head(worktree)
        commit_sha = session.data.get("commitSha")
        if not commit_sha and worktree_head != session.data["baseSha"]:
            manifest = self._manifest_for_session(session)
            task = manifest.get(session.data["taskId"])
            if task.status == "completed":
                session.data["commitSha"] = worktree_head
                session.save()
                commit_sha = worktree_head
                self.journal.record(
                    "commit_recovered", taskId=task.id, commitSha=worktree_head
                )
        elif not commit_sha:
            manifest = self._manifest_for_session(session)
            task = manifest.get(session.data["taskId"])
            if task.status == "completed":
                task.status = "in_review"
                manifest.save()
                self.journal.record(
                    "precommit_state_recovered", taskId=task.id, nextStatus="in_review"
                )
        if commit_sha and not session.data.get("merged"):
            if self.git.head() == commit_sha:
                session.data["merged"] = True
                session.save()
                self.journal.record(
                    "merge_recovered", taskId=session.data["taskId"], commitSha=commit_sha
                )
                self._cleanup_session(session)
                return
            self.ui.info(
                f"Recovering the confirmed commit for {session.data['taskId']}", "♻️ "
            )
            self._merge_and_cleanup(session)

    def _current_work(self) -> Tuple[Optional[Session], Optional[Manifest], Optional[Task]]:
        sessions = self._active_sessions()
        if sessions:
            session = sessions[0]
            manifest = self._manifest_for_session(session)
            task = manifest.get(session.data["taskId"])
            return session, manifest, task
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
        )
        self.journal.record(
            "session_started",
            taskId=task.id,
            branch=branch,
            baseSha=base_sha,
            worktree=str(worktree),
        )
        return session

    def _manifest_for_session(self, session: Session) -> Manifest:
        worktree = Path(session.data["worktree"])
        return Manifest.load(worktree / self.manifest_relative, worktree)

    def _process_stage(self, session: Session, manifest: Manifest, task: Task) -> None:
        if task.status in {"ready", "planning", "needs_replan"}:
            self._planner(session, manifest, task)
        elif task.status in {"planned", "implementing", "needs_changes"}:
            self._implementer(session, manifest, task)
        elif task.status == "in_review":
            self._reviewer(session, manifest, task)
        else:
            raise ConfigError(f"Cannot process {task.id} from state {task.status}")

    def _planner(self, session: Session, manifest: Manifest, task: Task) -> None:
        if not self._begin_attempt(task, "planning", manifest):
            return
        task.status = "planning"
        manifest.save()
        try:
            with self.ui.step("🧠", f"Planner · {task.id}"):
                result = self._run_role(session, task, "planner", [f"{task.task_dir}/plan.md"])
            self._require_artifact(session, task, "plan.md")
        except AgentError as error:
            self._role_failure(task, "planning", "needs_replan", manifest, error)
            return
        status = result["status"]
        if status == "READY":
            task.status = "planned"
            task.raw.pop("blockReason", None)
            self.ui.success("Plan is ready and validated")
        elif status == "BLOCKED":
            task.status = "blocked"
            task.raw["blockReason"] = "planner"
            self.ui.warning("Planner reported a blocker")
        else:
            task.status = "blocked"
            task.raw["blockReason"] = "clarification"
            self.ui.warning("Planner needs clarification")
        manifest.save()
        self.journal.record("planner_finished", taskId=task.id, status=status)

    def _implementer(self, session: Session, manifest: Manifest, task: Task) -> None:
        if not self._begin_attempt(task, "implementation", manifest):
            return
        task.status = "implementing"
        manifest.save()
        allowed = [*task.allowed_paths, f"{task.task_dir}/progress.md"]
        context = "Resolve every open REV-* finding from review.md and cite the IDs in progress.md."
        try:
            with self.ui.step("🛠️ ", f"Implementer · {task.id}"):
                result = self._run_role(session, task, "implementer", allowed, context)
            self._require_artifact(session, task, "progress.md")
        except AgentError as error:
            self._role_failure(task, "implementation", "needs_changes", manifest, error)
            return
        status = result["status"]
        if status == "BLOCKED":
            task.status = "blocked"
            task.raw["blockReason"] = "implementer"
            self.ui.warning("Implementer reported a blocker")
        elif status in {"IN_PROGRESS", "VERIFICATION_FAILED"}:
            task.status = "needs_changes"
            self.ui.warning("Implementation requires another pass")
        else:
            try:
                self._run_gates(session, task)
            except GateError as error:
                task.status = "needs_changes"
                task.raw["lastFailure"] = str(error)
                self.ui.warning(str(error))
            else:
                task.status = "in_review"
                task.raw.pop("lastFailure", None)
                self.ui.success("Implementation passed all quality gates")
        manifest.save()
        self.journal.record("implementer_finished", taskId=task.id, status=status)

    def _reviewer(self, session: Session, manifest: Manifest, task: Task) -> None:
        try:
            self._run_gates(session, task)
        except GateError as error:
            task.status = "needs_changes"
            task.raw["lastFailure"] = str(error)
            manifest.save()
            self.journal.record("review_precheck_failed", taskId=task.id, error=str(error))
            self.ui.warning("Code changed after the previous verification; returning to the implementer")
            return
        if not self._begin_attempt(task, "review", manifest):
            return
        allowed = [f"{task.task_dir}/review.md"]
        try:
            with self.ui.step("🔍", f"Reviewer · {task.id}"):
                result = self._run_role(
                    session,
                    task,
                    "reviewer",
                    allowed,
                    "Review the current uncommitted diff. Deterministic gates passed immediately before this review.",
                )
            self._require_artifact(session, task, "review.md")
        except AgentError as error:
            self._role_failure(task, "review", "in_review", manifest, error)
            return
        status = result["status"]
        if status == "PASS":
            try:
                self._run_gates(session, task)
            except GateError as error:
                task.status = "needs_changes"
                task.raw["lastFailure"] = str(error)
                self.ui.warning("Final verification failed; returning the task to the implementer")
            else:
                task.status = "completed"
                task.raw.pop("blockReason", None)
                task.raw.pop("lastFailure", None)
                self.ui.success("Independent review completed with PASS")
        elif status == "FAIL":
            task.status = "needs_changes"
            self.ui.warning("Reviewer found blocking issues")
        else:
            task.status = "needs_replan"
            self.ui.warning("Reviewer sent the task back for replanning")
        manifest.save()
        self.journal.record("reviewer_finished", taskId=task.id, status=status)

    def _begin_attempt(self, task: Task, stage: str, manifest: Manifest) -> bool:
        if task.attempts(stage) >= task.limit(stage):
            task.status = "failed"
            task.raw["blockReason"] = f"{stage}_attempts_exhausted"
            manifest.save()
            self.journal.record("attempts_exhausted", taskId=task.id, stage=stage)
            self.ui.error(f"{task.id}: exhausted the attempt limit for stage {stage}")
            return False
        task.increment(stage)
        manifest.save()
        return True

    def _role_failure(
        self,
        task: Task,
        stage: str,
        retry_status: str,
        manifest: Manifest,
        error: AgentError,
    ) -> None:
        task.raw["lastFailure"] = str(error)
        if task.attempts(stage) >= task.limit(stage):
            task.status = "failed"
            task.raw["blockReason"] = f"{stage}_attempts_exhausted"
        else:
            task.status = retry_status
        manifest.save()
        self.journal.record(
            "role_failed", taskId=task.id, stage=stage, error=str(error), nextStatus=task.status
        )
        self.ui.warning(str(error))

    def _run_role(
        self,
        session: Session,
        task: Task,
        role: str,
        allowed: List[str],
        context: str = "",
    ) -> Dict[str, Any]:
        worktree = Path(session.data["worktree"])
        sequence = session.next_sequence()
        result_dir = session.path.parent / "results"
        result_path = result_dir / f"{sequence:03d}-{role}.json"
        log_path = result_dir / f"{sequence:03d}-{role}.log"
        stage = {
            "planner": "planning",
            "implementer": "implementation",
            "reviewer": "review",
        }[role]
        model, reasoning_effort, escalated = self.config.agent.model_for(
            role, task.attempts(stage)
        )
        escalation_note = " · escalated" if escalated else ""
        self.ui.detail(
            f"Model: {model} · reasoning: {reasoning_effort}{escalation_note}"
        )
        before = self.git.file_snapshot(worktree)
        caught: Optional[Exception] = None
        result: Optional[Dict[str, Any]] = None
        try:
            result = self.agent.run(role, task, worktree, result_path, log_path, context)
            validate_role_result(role, result, task)
        except Exception as error:  # scope must still be checked after an agent failure
            caught = error
        after = self.git.file_snapshot(worktree)
        changed = self.git.changed_since(before, after)
        artifact_name = {
            "planner": "plan.md",
            "implementer": "progress.md",
            "reviewer": "review.md",
        }[role]
        role_artifact = f"{task.task_dir}/{artifact_name}"
        if role_artifact.startswith("./"):
            role_artifact = role_artifact[2:]
        workflow_names = {"task.md", "plan.md", "progress.md", "review.md"}
        workspace_prefix = task.workspace.strip("/")
        if workspace_prefix == ".":
            workspace_prefix = ""
        workflow_violations = sorted(
            path
            for path in changed
            if (not workspace_prefix or path.startswith(workspace_prefix + "/"))
            and Path(path).name in workflow_names
            and path != role_artifact
        )
        if workflow_violations:
            raise ScopeError(
                f"Role {role} modified artifacts belonging to another stage or task: "
                + ", ".join(workflow_violations)
            )
        protected = [
            ".git/**",
            ".ralph/**",
            "references/**",
            "**/AGENTS.md",
            self.manifest_relative,
            relative_path(self.config.root, self.config.prd_path),
            task.raw.get("prd", relative_path(self.config.root, self.config.prd_path)),
            f"{task.task_dir}/task.md",
        ]
        if role != "planner":
            protected.append(f"{task.task_dir}/plan.md")
        if role != "reviewer":
            protected.append(f"{task.task_dir}/review.md")
        if role != "implementer":
            protected.append(f"{task.task_dir}/progress.md")
        self.git.assert_scope(changed, allowed, role, protected)
        self.journal.record(
            "role_result",
            taskId=task.id,
            role=role,
            sequence=sequence,
            model=model,
            reasoningEffort=reasoning_effort,
            escalated=escalated,
            changedPaths=sorted(changed),
            resultPath=str(result_path),
            logPath=str(log_path),
        )
        if caught:
            if isinstance(caught, AgentError):
                raise caught
            raise AgentError(f"Role {role} failed: {caught}") from caught
        assert result is not None
        return result

    def _run_gates(self, session: Session, task: Task) -> None:
        worktree = Path(session.data["worktree"])
        sequence = session.next_sequence()
        log_dir = session.path.parent / "gates" / f"{sequence:03d}"
        before = self.git.file_snapshot(worktree)
        caught: Optional[Exception] = None
        results = []
        try:
            results = self.gates.run_all(task, worktree, log_dir)
        except Exception as error:
            caught = error
        after = self.git.file_snapshot(worktree)
        changed = self.git.changed_since(before, after)
        if changed:
            raise ScopeError(
                "Quality gate modified the worktree: " + ", ".join(sorted(changed))
            )
        if caught:
            if isinstance(caught, GateError):
                raise caught
            raise GateError(f"Cannot run quality gates: {caught}") from caught
        self.journal.record(
            "quality_gates_passed",
            taskId=task.id,
            results=[
                {
                    "name": result.name,
                    "command": result.command,
                    "exitCode": result.exit_code,
                    "elapsedSeconds": round(result.elapsed_seconds, 3),
                    "logPath": str(result.log_path),
                }
                for result in results
            ],
        )

    def _require_artifact(self, session: Session, task: Task, name: str) -> None:
        path = Path(session.data["worktree"]) / task.task_dir / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise AgentError(f"Role did not create the required file {path}")

    def _complete(self, session: Session, manifest: Manifest, task: Task) -> None:
        worktree = Path(session.data["worktree"])
        manifest.promote_dependencies()
        manifest.save()
        administrative = [
            self.manifest_relative,
            f"{task.task_dir}/plan.md",
            f"{task.task_dir}/progress.md",
            f"{task.task_dir}/review.md",
        ]
        changed = self.git.changed_paths(worktree)
        self.git.assert_scope(changed, [*task.allowed_paths, *administrative], "finalizer")
        with self.ui.step("📦", "Creating an atomic task commit"):
            commit_sha = self.git.commit_all(worktree, f"{task.id}: {task.title}")
            session.data["commitSha"] = commit_sha
            session.save()
            self.journal.record("commit_confirmed", taskId=task.id, commitSha=commit_sha)
        self._merge_and_cleanup(session)
        self.ui.success(f"{task.id} completed · {commit_sha[:8]}")

    def _merge_and_cleanup(self, session: Session) -> None:
        commit_sha = session.data.get("commitSha")
        if not commit_sha:
            raise GitError("Cannot merge a session without a confirmed commit")
        with self.ui.step("🔗", f"Fast-forwarding {session.data['baseBranch']}"):
            merged_sha = self.git.fast_forward(
                session.data["baseSha"],
                session.data["branch"],
                session.data["baseBranch"],
            )
            if merged_sha != commit_sha:
                raise GitError(
                    f"HEAD after merge is {merged_sha}; expected confirmed commit {commit_sha}"
                )
            session.data["merged"] = True
            session.save()
            self.journal.record(
                "merge_confirmed", taskId=session.data["taskId"], commitSha=commit_sha
            )
        self._cleanup_session(session)

    def _cleanup_session(self, session: Session) -> None:
        worktree = Path(session.data["worktree"])
        if worktree.exists():
            self.git.remove_worktree(worktree)
        if not self.config.git.keep_branches and self.git.branch_exists(session.data["branch"]):
            self.git.delete_branch(session.data["branch"])
        session.data["active"] = False
        session.save()
        self.journal.record("session_completed", taskId=session.data["taskId"])

    def _finish_without_task(
        self, manifest: Optional[Manifest], session: Optional[Session] = None
    ) -> int:
        if manifest is None:
            raise ConfigError("Cannot read task state")
        self._show_summary(manifest)
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
        else:
            self.ui.warning("No executable tasks remain; check dependencies and blockers.")
        return 3

    def _show_summary(self, manifest: Manifest) -> None:
        counts = manifest.counts()
        self.ui.summary(
            counts.get("completed", 0),
            len(manifest.tasks),
            counts.get("blocked", 0),
            counts.get("failed", 0),
        )
