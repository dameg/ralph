from __future__ import annotations

import argparse
import copy
import importlib.resources
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .config import Config, DEFAULT_CONFIG
from .errors import ConfigError, RalphError
from .git import Git
from .manifest import Manifest, blank_manifest
from .orchestrator import Orchestrator
from .ui import UI
from .util import atomic_write_json, read_json, relative_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ralph",
        description="Deterministyczna pętla planner → implementer → reviewer dla Codex.",
    )
    result.add_argument("--version", action="version", version=f"ralph {__version__}")
    subcommands = result.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Utwórz konfigurację i szablony")
    init.add_argument("--manifest", default="docs/tasks/manifest.json")
    init.add_argument("--prd", default="docs/prd.md")
    init.add_argument("--force", action="store_true", help="Nadpisz szablony Ralph")

    run = subcommands.add_parser("run", help="Uruchom lub wznów pętlę")
    run.add_argument("--max-iterations", type=int)
    run.add_argument("--verbose", action="store_true")
    run.add_argument("--color", choices=("auto", "always", "never"))

    status = subcommands.add_parser("status", help="Pokaż stan workflow")
    status.add_argument("--color", choices=("auto", "always", "never"))

    doctor = subcommands.add_parser("doctor", help="Sprawdź konfigurację i wymagania")
    doctor.add_argument("--color", choices=("auto", "always", "never"))
    return result


def main(arguments: Optional[List[str]] = None) -> int:
    args = parser().parse_args(arguments)
    root = _repository_root(Path.cwd())
    if args.command == "init":
        ui = UI()
        try:
            return _init(root, args.manifest, args.prd, args.force, ui)
        except RalphError as error:
            ui.error(str(error))
            return 1

    try:
        config = Config.load(root)
        color = args.color or config.color
        ui = UI(color=color, verbose=getattr(args, "verbose", False))
        if args.command == "run":
            if args.max_iterations is not None and args.max_iterations < 1:
                raise ConfigError("--max-iterations musi być dodatnie")
            return Orchestrator(config, ui).run(args.max_iterations)
        if args.command == "status":
            return _status(config, ui)
        if args.command == "doctor":
            return _doctor(config, ui)
    except (RalphError, OSError, ValueError, json.JSONDecodeError) as error:
        ui = locals().get("ui", UI(color="never"))
        ui.error(str(error))
        return 1
    return 1


def _repository_root(cwd: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError("Ralph musi być uruchomiony w repozytorium Git")
    return Path(result.stdout.strip()).resolve()


def _init(root: Path, manifest_value: str, prd_value: str, force: bool, ui: UI) -> int:
    config_path = root / ".ralph" / "config.json"
    manifest_path = (root / manifest_value).resolve()
    prd_path = (root / prd_value).resolve()
    for label, path in (("manifest", manifest_path), ("PRD", prd_path)):
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ConfigError(f"{label} musi znajdować się w repozytorium") from error
    if config_path.exists() and not force:
        raise ConfigError(f"Konfiguracja już istnieje: {config_path} (użyj --force)")

    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["manifest"] = relative_path(root, manifest_path)
    raw["prd"] = relative_path(root, prd_path)
    atomic_write_json(config_path, raw)
    _copy_templates(root, force)
    gitignore = root / ".ralph" / ".gitignore"
    if not gitignore.exists() or force:
        gitignore.write_text("runtime/\n", encoding="utf-8")

    if not manifest_path.exists():
        workspace = manifest_path.parent.relative_to(root).as_posix()
        atomic_write_json(manifest_path, blank_manifest(workspace))
    if not prd_path.exists():
        prd_path.parent.mkdir(parents=True, exist_ok=True)
        prd_path.write_text(
            "# Product requirements document\n\nTODO: opisz zakres modułu przed dodaniem zadań.\n",
            encoding="utf-8",
        )
    ui.banner("RALPH LOOP v3", "inicjalizacja zakończona")
    ui.success(f"Konfiguracja: {relative_path(root, config_path)}")
    ui.info(f"Manifest: {relative_path(root, manifest_path)}", "📋")
    ui.info(f"PRD: {relative_path(root, prd_path)}", "📝")
    ui.warning("Uzupełnij PRD i zadania w manifeście, a następnie zatwierdź pliki w Git.")
    return 0


def _copy_templates(root: Path, force: bool) -> None:
    source = importlib.resources.files("ralph_loop") / "templates"
    for group in ("prompts", "schemas"):
        target_dir = root / ".ralph" / group
        target_dir.mkdir(parents=True, exist_ok=True)
        for resource in (source / group).iterdir():
            if not resource.is_file():
                continue
            target = target_dir / resource.name
            if target.exists() and not force:
                continue
            target.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")


def _active_manifest(config: Config) -> tuple[Manifest, Optional[Dict[str, Any]]]:
    session_root = config.runtime_path / "sessions"
    active: List[Dict[str, Any]] = []
    if session_root.exists():
        for state_path in sorted(session_root.glob("*/state.json")):
            data = read_json(state_path)
            if data.get("active"):
                active.append(data)
    if len(active) > 1:
        raise ConfigError("Wykryto więcej niż jedną aktywną sesję")
    if active:
        worktree = Path(active[0]["worktree"])
        manifest = Manifest.load(worktree / relative_path(config.root, config.manifest_path), worktree)
        return manifest, active[0]
    return Manifest.load(config.manifest_path, config.root), None


def _status(config: Config, ui: UI) -> int:
    manifest, session = _active_manifest(config)
    ui.banner("RALPH STATUS", "aktualny stan workflow")
    if session:
        ui.info(
            f"Aktywne: {session['taskId']} · {session['branch']}",
            "🔄",
        )
    else:
        ui.info("Brak aktywnej sesji", "💤")
    if not manifest.tasks:
        ui.warning("Manifest nie zawiera zadań.")
    else:
        for task in manifest.tasks:
            icon = {
                "completed": "✅",
                "failed": "❌",
                "blocked": "⛔",
                "ready": "🟢",
            }.get(task.status, "🔄")
            print(f"{icon} {task.id:<12} {task.status:<14} {task.title}")
    counts = manifest.counts()
    ui.summary(
        counts.get("completed", 0),
        len(manifest.tasks),
        counts.get("blocked", 0),
        counts.get("failed", 0),
    )
    return 0


def _doctor(config: Config, ui: UI) -> int:
    ui.banner("RALPH DOCTOR", "kontrola środowiska")
    errors: List[str] = []
    for command in ("git", config.agent.command[0]):
        if shutil.which(command):
            ui.success(f"Dostępne polecenie: {command}")
        else:
            errors.append(f"Brak polecenia: {command}")
            ui.error(errors[-1])
    try:
        git = Git(config.root)
        git.ensure_repository()
        ui.success("Repozytorium Git jest poprawne")
        git.ensure_head()
        ui.success("Repozytorium ma bazowy commit")
        git.ensure_identity()
        ui.success("Autor commitów Git jest skonfigurowany")
        git.ensure_clean()
        ui.success("Główny worktree jest czysty")
    except RalphError as error:
        errors.append(str(error))
        ui.error(str(error))
    for path, label in ((config.prd_path, "PRD"), (config.manifest_path, "manifest")):
        if path.is_file():
            ui.success(f"{label}: {relative_path(config.root, path)}")
        else:
            errors.append(f"Brak {label}: {path}")
            ui.error(errors[-1])
    try:
        manifest = Manifest.load(config.manifest_path, config.root)
        ui.success(f"Kontrakt manifestu poprawny · {len(manifest.tasks)} zadań")
        for task in manifest.tasks:
            task_file = config.root / task.task_dir / "task.md"
            if not task_file.is_file():
                errors.append(f"Brak opisu {task.id}: {task_file}")
                ui.error(errors[-1])
    except RalphError as error:
        errors.append(str(error))
        ui.error(str(error))
    for role in ("planner", "implementer", "reviewer"):
        for group, suffix in (("prompts", ".md"), ("schemas", "-result.schema.json")):
            path = config.root / ".ralph" / group / f"{role}{suffix}"
            if not path.is_file():
                errors.append(f"Brak {path}")
                ui.error(errors[-1])
    if errors:
        ui.error(f"Wykryto {len(errors)} problemów")
        return 1
    ui.success("Środowisko gotowe do uruchomienia 🚀")
    return 0
