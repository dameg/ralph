from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .errors import ConfigError
from .util import ensure_command


ROLES = ("planner", "implementer", "reviewer")
REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}


@dataclass(frozen=True)
class RoleModelConfig:
    model: str
    reasoning_effort: str
    escalate_after_attempts: Optional[int] = None
    escalation_model: Optional[str] = None
    escalation_reasoning_effort: Optional[str] = None

    def select(self, attempt: int) -> tuple[str, str, bool]:
        escalated = (
            self.escalate_after_attempts is not None
            and attempt > self.escalate_after_attempts
        )
        if escalated:
            return (
                self.escalation_model or self.model,
                self.escalation_reasoning_effort or self.reasoning_effort,
                True,
            )
        return self.model, self.reasoning_effort, False


@dataclass(frozen=True)
class AgentConfig:
    command: List[str] = field(default_factory=lambda: ["codex", "exec"])
    sandbox: str = "workspace-write"
    network_access: bool = False
    timeout_seconds: int = 1800
    model: Optional[str] = None
    roles: Mapping[str, RoleModelConfig] = field(default_factory=dict)

    def model_for(self, role: str, attempt: int) -> tuple[str, str, bool]:
        profile = self.roles.get(role)
        if profile:
            return profile.select(attempt)
        if self.model:
            return self.model, "medium", False
        raise ConfigError(f"No model configuration found for role {role}")


@dataclass(frozen=True)
class GitConfig:
    branch_prefix: str = "ralph/"
    keep_branches: bool = False


@dataclass(frozen=True)
class Config:
    root: Path
    config_path: Path
    manifest_path: Path
    prd_path: Path
    runtime_path: Path
    max_iterations: int
    color: str
    agent: AgentConfig
    git: GitConfig

    @classmethod
    def load(cls, root: Path, path: Optional[Path] = None) -> "Config":
        root = root.resolve()
        config_path = (path or root / ".ralph" / "config.json").resolve()
        if not config_path.is_file():
            raise ConfigError(
                f"Configuration not found: {config_path}. Run `./ralph init` first."
            )
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"Cannot read {config_path}: {error}") from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ConfigError("Configuration must be a JSON object with version=1")

        def resolve(value: Any, label: str) -> Path:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{label} must be a relative path")
            candidate = (root / value).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise ConfigError(f"{label} must point inside the repository") from error
            return candidate

        agent_raw = raw.get("agent", {})
        git_raw = raw.get("git", {})
        if not isinstance(agent_raw, dict) or not isinstance(git_raw, dict):
            raise ConfigError("agent and git must be JSON objects")
        try:
            command = ensure_command(agent_raw.get("command", ["codex", "exec"]), "agent.command")
        except ValueError as error:
            raise ConfigError(str(error)) from error
        sandbox = agent_raw.get("sandbox", "workspace-write")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ConfigError("agent.sandbox must be read-only or workspace-write")
        timeout = agent_raw.get("timeoutSeconds", 1800)
        max_iterations = raw.get("maxIterations", 30)
        if not isinstance(timeout, int) or timeout < 1:
            raise ConfigError("agent.timeoutSeconds must be a positive integer")
        if not isinstance(max_iterations, int) or max_iterations < 1:
            raise ConfigError("maxIterations must be a positive integer")
        color = raw.get("ui", {}).get("color", "auto") if isinstance(raw.get("ui", {}), dict) else "auto"
        if color not in {"auto", "always", "never"}:
            raise ConfigError("ui.color must be auto, always, or never")
        branch_prefix = git_raw.get("branchPrefix", "ralph/")
        if not isinstance(branch_prefix, str) or not branch_prefix:
            raise ConfigError("git.branchPrefix cannot be empty")
        model = agent_raw.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ConfigError("agent.model must be a non-empty string")
        roles = _load_role_models(agent_raw.get("roles"), model)

        return cls(
            root=root,
            config_path=config_path,
            manifest_path=resolve(raw.get("manifest"), "manifest"),
            prd_path=resolve(raw.get("prd"), "prd"),
            runtime_path=resolve(raw.get("runtime", ".ralph/runtime"), "runtime"),
            max_iterations=max_iterations,
            color=color,
            agent=AgentConfig(
                command=command,
                sandbox=sandbox,
                network_access=bool(agent_raw.get("networkAccess", False)),
                timeout_seconds=timeout,
                model=model,
                roles=roles,
            ),
            git=GitConfig(
                branch_prefix=branch_prefix,
                keep_branches=bool(git_raw.get("keepBranches", False)),
            ),
        )


DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "manifest": "docs/tasks/manifest.json",
    "prd": "docs/prd.md",
    "runtime": ".ralph/runtime",
    "maxIterations": 30,
    "agent": {
        "command": ["codex", "exec"],
        "sandbox": "workspace-write",
        "networkAccess": False,
        "timeoutSeconds": 1800,
        "roles": {
            "planner": {
                "model": "gpt-5.6-terra",
                "reasoningEffort": "medium",
                "escalateAfterAttempts": 1,
                "escalationModel": "gpt-5.6-sol",
                "escalationReasoningEffort": "high",
            },
            "implementer": {
                "model": "gpt-5.6-terra",
                "reasoningEffort": "medium",
                "escalateAfterAttempts": 2,
                "escalationModel": "gpt-5.6-sol",
                "escalationReasoningEffort": "high",
            },
            "reviewer": {
                "model": "gpt-5.6-terra",
                "reasoningEffort": "high",
                "escalateAfterAttempts": 1,
                "escalationModel": "gpt-5.6-sol",
                "escalationReasoningEffort": "high",
            },
        },
    },
    "git": {"branchPrefix": "ralph/", "keepBranches": False},
    "ui": {"color": "auto"},
}


def _load_role_models(value: Any, global_model: Optional[str]) -> Dict[str, RoleModelConfig]:
    defaults = DEFAULT_CONFIG["agent"]["roles"]
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("agent.roles must be a JSON object")
    unknown = sorted(set(value) - set(ROLES))
    if unknown:
        raise ConfigError(f"agent.roles contains unknown roles: {', '.join(unknown)}")

    result: Dict[str, RoleModelConfig] = {}
    for role in ROLES:
        if global_model and role not in value:
            result[role] = RoleModelConfig(
                model=global_model,
                reasoning_effort="medium",
            )
            continue
        raw = value.get(role, {})
        if not isinstance(raw, dict):
            raise ConfigError(f"agent.roles.{role} must be a JSON object")
        default = defaults[role]
        model = raw.get("model", global_model or default["model"])
        effort = raw.get("reasoningEffort", default["reasoningEffort"])
        escalate_after = raw.get(
            "escalateAfterAttempts", default.get("escalateAfterAttempts")
        )
        escalation_model = raw.get("escalationModel", default.get("escalationModel"))
        escalation_effort = raw.get(
            "escalationReasoningEffort", default.get("escalationReasoningEffort")
        )
        if not isinstance(model, str) or not model:
            raise ConfigError(f"agent.roles.{role}.model must be a non-empty string")
        if effort not in REASONING_EFFORTS:
            raise ConfigError(
                f"agent.roles.{role}.reasoningEffort must be one of: "
                + ", ".join(sorted(REASONING_EFFORTS))
            )
        if escalate_after is not None and (
            not isinstance(escalate_after, int) or escalate_after < 1
        ):
            raise ConfigError(
                f"agent.roles.{role}.escalateAfterAttempts must be a positive integer"
            )
        if escalation_model is not None and (
            not isinstance(escalation_model, str) or not escalation_model
        ):
            raise ConfigError(
                f"agent.roles.{role}.escalationModel must be a non-empty string"
            )
        if escalation_effort is not None and escalation_effort not in REASONING_EFFORTS:
            raise ConfigError(
                f"agent.roles.{role}.escalationReasoningEffort must be one of: "
                + ", ".join(sorted(REASONING_EFFORTS))
            )
        result[role] = RoleModelConfig(
            model=model,
            reasoning_effort=effort,
            escalate_after_attempts=escalate_after,
            escalation_model=escalation_model,
            escalation_reasoning_effort=escalation_effort,
        )
    return result
