from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol

from .config import Config
from .errors import AgentError
from .manifest import Task
from .util import stable_env


ROLE_STATUSES = {
    "planner": {"READY", "BLOCKED", "NEEDS_CLARIFICATION"},
    "implementer": {
        "IMPLEMENTATION_COMPLETE",
        "IN_PROGRESS",
        "BLOCKED",
        "VERIFICATION_FAILED",
    },
    "reviewer": {"PASS", "FAIL", "NEEDS_REPLAN"},
}


class Agent(Protocol):
    def run(
        self,
        role: str,
        task: Task,
        root: Path,
        result_path: Path,
        log_path: Path,
        context: str = "",
    ) -> Dict[str, Any]:
        ...


class CodexAgent:
    def __init__(self, config: Config) -> None:
        self.config = config

    def run(
        self,
        role: str,
        task: Task,
        root: Path,
        result_path: Path,
        log_path: Path,
        context: str = "",
    ) -> Dict[str, Any]:
        prompt_path = root / ".ralph" / "prompts" / f"{role}.md"
        schema_path = root / ".ralph" / "schemas" / f"{role}-result.schema.json"
        task_file = root / task.task_dir / "task.md"
        for required in (prompt_path, schema_path, task_file):
            if not required.is_file():
                raise AgentError(f"Required file for role {role} not found: {required}")

        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            *self.config.agent.command,
            "--ephemeral",
            "--sandbox",
            self.config.agent.sandbox,
            "--cd",
            str(root),
            "--config",
            "sandbox_workspace_write.network_access="
            + ("true" if self.config.agent.network_access else "false"),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "--color",
            "never",
        ]
        if self.config.agent.model:
            command.extend(["--model", self.config.agent.model])
        command.append("-")
        prompt = self._prompt(role, task, context)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    command,
                    cwd=str(root),
                    env=stable_env(),
                    input=prompt,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=self.config.agent.timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            raise AgentError(
                f"Role {role} exceeded the {self.config.agent.timeout_seconds}s timeout; log: {log_path}"
            ) from error
        except OSError as error:
            raise AgentError(f"Cannot start the agent: {error}") from error
        if result.returncode != 0:
            raise AgentError(
                f"Role {role} exited with code {result.returncode}; log: {log_path}"
            )
        if not result_path.is_file():
            raise AgentError(f"Role {role} did not return a JSON result; log: {log_path}")
        try:
            with result_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise AgentError(f"Invalid JSON from role {role}: {error}; log: {log_path}") from error
        validate_role_result(role, payload, task)
        return payload

    def _prompt(self, role: str, task: Task, context: str) -> str:
        artifacts = {
            "planner": "plan.md",
            "implementer": "progress.md",
            "reviewer": "review.md",
        }
        restrictions = {
            "planner": "You may modify only plan.md in the task directory.",
            "implementer": (
                "You may modify progress.md and the paths allowed by the task contract. "
                "Do not modify task.md, plan.md, review.md, the manifest, or references/."
            ),
            "reviewer": "You may modify only review.md in the task directory. Do not fix the code.",
        }
        return f"""Follow all applicable AGENTS.md files.

You are the {role} for exactly one task: {task.id} — {task.title}.

Read, in this order:
1. .ralph/prompts/{role}.md (the complete role contract),
2. {task.task_dir}/task.md,
3. the PRD configured in .ralph/config.json,
4. repository instructions and relevant source files,
5. existing plan.md, progress.md, and review.md when relevant.

Hard boundaries:
- Work only on {task.id}.
- {restrictions[role]}
- Never modify the manifest or .ralph configuration.
- Never create Git commits or branches.
- Never modify anything under references/.
- Write the human-readable artifact to {task.task_dir}/{artifacts[role]}.
- Your final response must be only the JSON object required by the supplied schema.

Machine contract:
{json.dumps(task.raw['contract'], ensure_ascii=False, indent=2)}

Additional recovery context:
{context or 'None.'}
"""


def validate_role_result(role: str, payload: Any, task: Task) -> None:
    if role not in ROLE_STATUSES:
        raise AgentError(f"Unknown role: {role}")
    if not isinstance(payload, dict):
        raise AgentError(f"The {role} result must be a JSON object")
    status = payload.get("status")
    if status not in ROLE_STATUSES[role]:
        expected = ", ".join(sorted(ROLE_STATUSES[role]))
        raise AgentError(f"Role {role} returned status {status!r}; expected: {expected}")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise AgentError(f"Role {role} must return a non-empty summary")
    if role in {"implementer", "reviewer"}:
        evidence = payload.get("acceptanceCriteria")
        if not isinstance(evidence, list):
            raise AgentError(f"Role {role} must evaluate acceptanceCriteria")
        by_id: Dict[str, Mapping[str, Any]] = {}
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AgentError(f"Role {role} returned an invalid acceptanceCriteria entry")
            if item["id"] in by_id:
                raise AgentError(f"Role {role} returned duplicate criterion {item['id']}")
            by_id[item["id"]] = item
        missing = [criterion for criterion in task.acceptance_ids if criterion not in by_id]
        extra = sorted(set(by_id) - set(task.acceptance_ids))
        if missing or extra:
            raise AgentError(
                f"Role {role} returned an incomplete AC set; missing={missing}, extra={extra}"
            )
        for criterion_id, item in by_id.items():
            if item.get("status") not in {"PASS", "FAIL"}:
                raise AgentError(f"{role}: {criterion_id} has an invalid status")
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                raise AgentError(f"{role}: {criterion_id} has no evidence")
        if status in {"IMPLEMENTATION_COMPLETE", "PASS"}:
            failed = [key for key, item in by_id.items() if item.get("status") != "PASS"]
            if failed:
                raise AgentError(f"Status {status} requires PASS for every AC; failed: {failed}")
    if role == "reviewer":
        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise AgentError("Reviewer must return a findings array")
        ids = set()
        for finding in findings:
            if not isinstance(finding, dict):
                raise AgentError("Reviewer returned an invalid finding")
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not finding_id.startswith("REV-"):
                raise AgentError("Every finding must have a stable REV-* identifier")
            if finding_id in ids:
                raise AgentError(f"Duplicate finding: {finding_id}")
            ids.add(finding_id)
            if finding.get("severity") not in {"low", "medium", "high", "critical"}:
                raise AgentError(f"{finding_id} has an invalid severity")
            if finding.get("status") not in {"open", "resolved"}:
                raise AgentError(f"{finding_id} has an invalid status")
        if status == "PASS" and any(item.get("status") == "open" for item in findings):
            raise AgentError("Reviewer cannot return PASS with open findings")
