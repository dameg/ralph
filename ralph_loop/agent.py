from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Union

from .config import Config
from .errors import AgentContractError, AgentError, AgentInfrastructureError
from .manifest import Task
from .util import ensure_command, ensure_string_list, relative_path, stable_env


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
ROLE_FIELDS = {
    "planner": {"status", "summary", "filesPlanned", "verificationCommands"},
    "implementer": {
        "status",
        "summary",
        "changedFiles",
        "verification",
        "acceptanceCriteria",
    },
    "reviewer": {"status", "summary", "acceptanceCriteria", "findings"},
}


def _require_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise AgentContractError(
            f"{label} fields do not match the contract; missing={missing}, extra={extra}"
        )


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cachedInputTokens": self.cached_input_tokens,
        }


@dataclass(frozen=True)
class AgentResult:
    payload: Dict[str, Any]
    usage: Optional[TokenUsage] = None


class Agent(Protocol):
    def run(
        self,
        role: str,
        task: Task,
        root: Path,
        result_path: Path,
        log_path: Path,
        context: str = "",
        invocation: int = 1,
    ) -> Union[Dict[str, Any], AgentResult]:
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
        invocation: int = 1,
    ) -> AgentResult:
        prompt_path = root / ".ralph" / "prompts" / f"{role}.md"
        schema_path = root / ".ralph" / "schemas" / f"{role}-result.schema.json"
        task_file = root / task.task_dir / "task.md"
        for required in (prompt_path, schema_path, task_file):
            if not required.is_file():
                raise AgentError(f"Required file for role {role} not found: {required}")

        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        model, reasoning_effort, _ = self.config.agent.model_for(role, invocation)
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
        if self._is_codex_command():
            command.append("--json")
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
            raise AgentContractError(f"Role {role} did not return a JSON result; log: {log_path}")
        try:
            with result_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise AgentContractError(
                f"Invalid JSON from role {role}: {error}; log: {log_path}"
            ) from error
        validate_role_result(role, payload, task)
        return AgentResult(payload, parse_usage_log(log_path) if self._is_codex_command() else None)

    def _is_codex_command(self) -> bool:
        return bool(self.config.agent.command) and Path(self.config.agent.command[0]).name == "codex"

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
        prd_path = task.effective_prd(default_prd)
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


def parse_usage_log(path: Path) -> Optional[TokenUsage]:
    """Return the final token usage reported by a Codex JSONL invocation."""
    usage: Optional[TokenUsage] = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for candidate in _usage_objects(event):
            parsed = _token_usage(candidate)
            if parsed is not None:
                usage = parsed
    return usage


def _usage_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _usage_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _usage_objects(child)


def _token_usage(value: Mapping[str, Any]) -> Optional[TokenUsage]:
    def number(*keys: str) -> Optional[int]:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int) and candidate >= 0:
                return candidate
        return None

    input_tokens = number("input_tokens", "inputTokens")
    output_tokens = number("output_tokens", "outputTokens")
    cached = number("cached_input_tokens", "cachedInputTokens")
    if input_tokens is None or output_tokens is None:
        return None
    return TokenUsage(input_tokens, output_tokens, cached or 0)


def validate_role_result(role: str, payload: Any, task: Task) -> None:
    if role not in ROLE_STATUSES:
        raise AgentContractError(f"Unknown role: {role}")
    if not isinstance(payload, dict):
        raise AgentContractError(f"The {role} result must be a JSON object")
    _require_fields(payload, ROLE_FIELDS[role], f"Role {role} result")
    status = payload.get("status")
    if status not in ROLE_STATUSES[role]:
        expected = ", ".join(sorted(ROLE_STATUSES[role]))
        raise AgentContractError(f"Role {role} returned status {status!r}; expected: {expected}")
    if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
        raise AgentContractError(f"Role {role} must return a non-empty summary")
    if role == "planner":
        try:
            ensure_string_list(payload["filesPlanned"], "planner.filesPlanned")
        except ValueError as error:
            raise AgentContractError(str(error)) from error
        commands = payload["verificationCommands"]
        if not isinstance(commands, list):
            raise AgentContractError("planner.verificationCommands must be an array")
        for index, command in enumerate(commands):
            try:
                ensure_command(command, f"planner.verificationCommands[{index}]")
            except ValueError as error:
                raise AgentContractError(str(error)) from error
    if role == "implementer":
        try:
            ensure_string_list(payload["changedFiles"], "implementer.changedFiles")
        except ValueError as error:
            raise AgentContractError(str(error)) from error
        verification = payload["verification"]
        if not isinstance(verification, list):
            raise AgentContractError("implementer.verification must be an array")
        for index, item in enumerate(verification):
            if not isinstance(item, dict):
                raise AgentContractError(
                    f"implementer.verification[{index}] must be an object"
                )
            _require_fields(
                item,
                {"command", "exitCode", "summary"},
                f"implementer.verification[{index}]",
            )
            try:
                ensure_command(item["command"], f"implementer.verification[{index}].command")
            except ValueError as error:
                raise AgentContractError(str(error)) from error
            if not _is_integer(item["exitCode"]):
                raise AgentContractError(
                    f"implementer.verification[{index}].exitCode must be an integer"
                )
            if not isinstance(item["summary"], str):
                raise AgentContractError(
                    f"implementer.verification[{index}].summary must be a string"
                )
    if role in {"implementer", "reviewer"}:
        evidence = payload.get("acceptanceCriteria")
        if not isinstance(evidence, list):
            raise AgentContractError(f"Role {role} must evaluate acceptanceCriteria")
        by_id: Dict[str, Mapping[str, Any]] = {}
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise AgentContractError(f"Role {role} returned an invalid acceptanceCriteria entry")
            _require_fields(
                item,
                {"id", "status", "evidence"},
                f"Role {role} acceptanceCriteria entry",
            )
            if not item["id"].startswith("AC-"):
                raise AgentContractError(f"Role {role} returned an invalid AC identifier")
            if item["id"] in by_id:
                raise AgentContractError(f"Role {role} returned duplicate criterion {item['id']}")
            by_id[item["id"]] = item
        missing = [criterion for criterion in task.acceptance_ids if criterion not in by_id]
        extra = sorted(set(by_id) - set(task.acceptance_ids))
        if missing or extra:
            raise AgentContractError(
                f"Role {role} returned an incomplete AC set; missing={missing}, extra={extra}"
            )
        for criterion_id, item in by_id.items():
            if item.get("status") not in {"PASS", "FAIL"}:
                raise AgentContractError(f"{role}: {criterion_id} has an invalid status")
            if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
                raise AgentContractError(f"{role}: {criterion_id} has no evidence")
        if status in {"IMPLEMENTATION_COMPLETE", "PASS"}:
            failed = [key for key, item in by_id.items() if item.get("status") != "PASS"]
            if failed:
                raise AgentContractError(
                    f"Status {status} requires PASS for every AC; failed: {failed}"
                )
    if role == "reviewer":
        findings = payload.get("findings")
        if not isinstance(findings, list):
            raise AgentContractError("Reviewer must return a findings array")
        ids = set()
        for finding in findings:
            if not isinstance(finding, dict):
                raise AgentContractError("Reviewer returned an invalid finding")
            _require_fields(
                finding,
                {
                    "id",
                    "severity",
                    "file",
                    "description",
                    "expectedBehavior",
                    "status",
                },
                "Reviewer finding",
            )
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not finding_id.startswith("REV-"):
                raise AgentContractError("Every finding must have a stable REV-* identifier")
            if finding_id in ids:
                raise AgentContractError(f"Duplicate finding: {finding_id}")
            ids.add(finding_id)
            if finding.get("severity") not in {"low", "medium", "high", "critical"}:
                raise AgentContractError(f"{finding_id} has an invalid severity")
            if finding.get("status") not in {"open", "resolved"}:
                raise AgentContractError(f"{finding_id} has an invalid status")
            if not isinstance(finding.get("file"), str):
                raise AgentContractError(f"{finding_id}.file must be a string")
            for field in ("description", "expectedBehavior"):
                if not isinstance(finding.get(field), str) or not finding[field].strip():
                    raise AgentContractError(f"{finding_id}.{field} cannot be empty")
        if status == "PASS" and any(item.get("status") == "open" for item in findings):
            raise AgentContractError("Reviewer cannot return PASS with open findings")
        if status == "FAIL" and not any(item.get("status") == "open" for item in findings):
            raise AgentContractError("Reviewer FAIL requires at least one open finding")
