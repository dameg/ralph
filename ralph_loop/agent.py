from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol

from .config import Config
from .errors import AgentError, AgentInfrastructureError
from .manifest import Task
from .util import matches_any, relative_path, stable_env


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
        stage = {
            "planner": "planning",
            "implementer": "implementation",
            "reviewer": "review",
        }[role]
        model, reasoning_effort, _ = self.config.agent.model_for(
            role, task.attempts(stage)
        )
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
            "--model",
            model,
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
        ]
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
            raise AgentInfrastructureError(
                f"Role {role} exceeded the {self.config.agent.timeout_seconds}s timeout; log: {log_path}"
            ) from error
        except OSError as error:
            raise AgentInfrastructureError(f"Cannot start the agent: {error}") from error
        if result.returncode != 0:
            raise AgentInfrastructureError(
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
        default_prd = relative_path(self.config.root, self.config.prd_path)
        prd_path = task.raw.get("prd", default_prd)
        return f"""Follow all applicable AGENTS.md files.

You are the {role} for exactly one task: {task.id} — {task.title}.

Read, in this order:
1. .ralph/prompts/{role}.md (the complete role contract),
2. {task.task_dir}/task.md,
3. {prd_path}, the PRD assigned to this task,
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
    if not all(isinstance(key, str) for key in payload):
        raise AgentError(f"The {role} result contains a non-string field name")
    required_fields = {
        "planner": {"status", "summary", "filesPlanned", "verificationCommands"},
        "implementer": {
            "status",
            "summary",
            "changedFiles",
            "verification",
            "acceptanceCriteria",
        },
        "reviewer": {"status", "summary", "acceptanceCriteria", "findings"},
    }[role]
    missing_fields = sorted(required_fields - set(payload))
    extra_fields = sorted(set(payload) - required_fields)
    if missing_fields or extra_fields:
        raise AgentError(
            f"Role {role} returned an invalid field set; "
            f"missing={missing_fields}, extra={extra_fields}"
        )
    status = payload.get("status")
    if status not in ROLE_STATUSES[role]:
        expected = ", ".join(sorted(ROLE_STATUSES[role]))
        raise AgentError(f"Role {role} returned status {status!r}; expected: {expected}")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise AgentError(f"Role {role} must return a non-empty summary")
    if role == "planner":
        planned_files = _string_array(payload["filesPlanned"], "planner.filesPlanned")
        outside_scope = sorted(
            path for path in planned_files if not matches_any(path, task.allowed_paths)
        )
        if outside_scope:
            raise AgentError(
                "Planner proposed files outside the task scope: " + ", ".join(outside_scope)
            )
        verification_commands = _command_array(
            payload["verificationCommands"], "planner.verificationCommands"
        )
        expected_commands = [list(gate["command"]) for gate in task.gates]
        if verification_commands != expected_commands:
            raise AgentError(
                "Planner verificationCommands must exactly match the task quality gates"
            )
    if role == "implementer":
        _string_array(payload["changedFiles"], "implementer.changedFiles")
        verification = payload["verification"]
        if not isinstance(verification, list):
            raise AgentError("implementer.verification must be an array")
        for index, item in enumerate(verification):
            label = f"implementer.verification[{index}]"
            _exact_fields(item, {"command", "exitCode", "summary"}, label)
            _command(item["command"], f"{label}.command")
            if not isinstance(item["exitCode"], int) or isinstance(item["exitCode"], bool):
                raise AgentError(f"{label}.exitCode must be an integer")
            if not isinstance(item["summary"], str):
                raise AgentError(f"{label}.summary must be a string")
    if role in {"implementer", "reviewer"}:
        evidence = payload.get("acceptanceCriteria")
        if not isinstance(evidence, list):
            raise AgentError(f"Role {role} must evaluate acceptanceCriteria")
        by_id: Dict[str, Mapping[str, Any]] = {}
        for index, item in enumerate(evidence):
            label = f"{role}.acceptanceCriteria[{index}]"
            _exact_fields(item, {"id", "status", "evidence"}, label)
            if not isinstance(item.get("id"), str):
                raise AgentError(f"{label}.id must be a string")
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
        for index, finding in enumerate(findings):
            label = f"reviewer.findings[{index}]"
            _exact_fields(
                finding,
                {"id", "severity", "file", "description", "expectedBehavior", "status"},
                label,
            )
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
            if not isinstance(finding.get("file"), str):
                raise AgentError(f"{finding_id}.file must be a string")
            for field in ("description", "expectedBehavior"):
                if not isinstance(finding.get(field), str) or not finding[field].strip():
                    raise AgentError(f"{finding_id}.{field} must be a non-empty string")
        if status == "PASS" and any(item.get("status") == "open" for item in findings):
            raise AgentError("Reviewer cannot return PASS with open findings")


def _exact_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise AgentError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise AgentError(f"{label} contains a non-string field name")
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise AgentError(f"{label} has invalid fields; missing={missing}, extra={extra}")


def _string_array(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentError(f"{label} must be an array of strings")
    return list(value)


def _command(value: Any, label: str) -> List[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise AgentError(f"{label} must be a non-empty argument array")
    return list(value)


def _command_array(value: Any, label: str) -> List[List[str]]:
    if not isinstance(value, list):
        raise AgentError(f"{label} must be an array")
    return [_command(command, f"{label}[{index}]") for index, command in enumerate(value)]
