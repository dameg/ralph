from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError
from .util import ensure_command


@dataclass(frozen=True)
class AgentConfig:
    command: List[str] = field(default_factory=lambda: ["codex", "exec"])
    sandbox: str = "workspace-write"
    network_access: bool = False
    timeout_seconds: int = 1800
    model: Optional[str] = None


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
                f"Brak konfiguracji: {config_path}. Uruchom najpierw `./ralph init`."
            )
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"Nie można odczytać {config_path}: {error}") from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ConfigError("Konfiguracja musi być obiektem JSON z version=1")

        def resolve(value: Any, label: str) -> Path:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{label} musi być ścieżką względną")
            candidate = (root / value).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise ConfigError(f"{label} musi wskazywać wnętrze repozytorium") from error
            return candidate

        agent_raw = raw.get("agent", {})
        git_raw = raw.get("git", {})
        if not isinstance(agent_raw, dict) or not isinstance(git_raw, dict):
            raise ConfigError("agent i git muszą być obiektami JSON")
        try:
            command = ensure_command(agent_raw.get("command", ["codex", "exec"]), "agent.command")
        except ValueError as error:
            raise ConfigError(str(error)) from error
        sandbox = agent_raw.get("sandbox", "workspace-write")
        if sandbox not in {"read-only", "workspace-write"}:
            raise ConfigError("agent.sandbox może być tylko read-only lub workspace-write")
        timeout = agent_raw.get("timeoutSeconds", 1800)
        max_iterations = raw.get("maxIterations", 30)
        if not isinstance(timeout, int) or timeout < 1:
            raise ConfigError("agent.timeoutSeconds musi być dodatnią liczbą całkowitą")
        if not isinstance(max_iterations, int) or max_iterations < 1:
            raise ConfigError("maxIterations musi być dodatnią liczbą całkowitą")
        color = raw.get("ui", {}).get("color", "auto") if isinstance(raw.get("ui", {}), dict) else "auto"
        if color not in {"auto", "always", "never"}:
            raise ConfigError("ui.color musi mieć wartość auto, always lub never")
        branch_prefix = git_raw.get("branchPrefix", "ralph/")
        if not isinstance(branch_prefix, str) or not branch_prefix:
            raise ConfigError("git.branchPrefix nie może być pusty")
        model = agent_raw.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ConfigError("agent.model musi być niepustym napisem")

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
    },
    "git": {"branchPrefix": "ralph/", "keepBranches": False},
    "ui": {"color": "auto"},
}
