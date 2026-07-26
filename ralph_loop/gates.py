from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .errors import GateError
from .manifest import Task
from .ui import UI
from .util import stable_env


@dataclass(frozen=True)
class GateResult:
    name: str
    command: List[str]
    exit_code: int
    elapsed_seconds: float
    log_path: Path


class GateRunner:
    def __init__(self, ui: UI) -> None:
        self.ui = ui

    def run_all(self, task: Task, root: Path, log_dir: Path) -> List[GateResult]:
        results: List[GateResult] = []
        log_dir.mkdir(parents=True, exist_ok=True)
        for index, gate in enumerate(task.gates, start=1):
            name = gate["name"]
            command = list(gate["command"])
            timeout = int(gate.get("timeoutSeconds", 600))
            log_path = log_dir / f"{index:02d}-{_safe_name(name)}.log"
            with self.ui.step("🧪", f"Quality gate: {name}"):
                started = time.monotonic()
                try:
                    result = subprocess.run(
                        command,
                        cwd=str(root),
                        env=stable_env(),
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        check=False,
                    )
                    output = result.stdout or ""
                    exit_code = result.returncode
                except subprocess.TimeoutExpired as error:
                    output = _timeout_output(error)
                    exit_code = 124
                except OSError as error:
                    output = str(error)
                    exit_code = 127
                elapsed = time.monotonic() - started
                log_path.write_text(
                    f"$ {shlex.join(command)}\n\n{output}\n\nexit_code={exit_code}\n",
                    encoding="utf-8",
                )
                gate_result = GateResult(name, command, exit_code, elapsed, log_path)
                results.append(gate_result)
                if exit_code != 0:
                    raise GateError(
                        f"Quality gate '{name}' failed with exit code {exit_code}; log: {log_path}"
                    )
        return results


def _safe_name(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-") or "gate"


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    output = error.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return f"{output}\nTimed out after {error.timeout}s"
