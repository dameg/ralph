from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator, Optional


class UI:
    COLORS = {
        "dim": "\033[2m",
        "blue": "\033[36m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }

    def __init__(self, color: str = "auto", verbose: bool = False) -> None:
        self.verbose = verbose
        self.use_color = color == "always" or (
            color == "auto" and sys.stdout.isatty() and not os.getenv("NO_COLOR")
        )

    def paint(self, text: str, style: str) -> str:
        if not self.use_color:
            return text
        return f"{self.COLORS[style]}{text}{self.COLORS['reset']}"

    def banner(self, title: str, subtitle: str) -> None:
        width = 68
        print()
        print(self.paint("━" * width, "blue"))
        print(f"  🤖 {self.paint(title, 'bold')}")
        print(f"     {self.paint(subtitle, 'dim')}")
        print(self.paint("━" * width, "blue"))

    def info(self, message: str, icon: str = "ℹ️ ") -> None:
        print(f"{icon} {message}")

    def success(self, message: str) -> None:
        print(f"✅ {self.paint(message, 'green')}")

    def warning(self, message: str) -> None:
        print(f"⚠️  {self.paint(message, 'yellow')}")

    def error(self, message: str) -> None:
        print(f"❌ {self.paint(message, 'red')}", file=sys.stderr)

    def detail(self, message: str) -> None:
        if self.verbose:
            print(f"   {self.paint(message, 'dim')}")

    def iteration(self, current: int, maximum: int, task_id: str, title: str) -> None:
        print()
        print(self.paint(f"── Iteracja {current}/{maximum} · {task_id} ──", "blue"))
        print(f"   {title}")

    @contextmanager
    def step(self, icon: str, label: str) -> Iterator[None]:
        started = time.monotonic()
        print(f"{icon}  {label}…", flush=True)
        try:
            yield
        except Exception:
            elapsed = time.monotonic() - started
            print(f"   {self.paint(f'niepowodzenie ({elapsed:.1f}s)', 'red')}")
            raise
        else:
            elapsed = time.monotonic() - started
            print(f"   {self.paint(f'gotowe ({elapsed:.1f}s)', 'green')}")

    def summary(self, completed: int, total: int, blocked: int, failed: int) -> None:
        print()
        print(self.paint("━" * 68, "blue"))
        print(
            f"📊 {completed}/{total} ukończonych"
            f"  ·  ⛔ {blocked} zablokowanych"
            f"  ·  ❌ {failed} nieudanych"
        )
        print(self.paint("━" * 68, "blue"))


class NullUI(UI):
    def __init__(self) -> None:
        super().__init__(color="never", verbose=False)

    def banner(self, title: str, subtitle: str) -> None:
        pass

    def info(self, message: str, icon: str = "ℹ️ ") -> None:
        pass

    def success(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def detail(self, message: str) -> None:
        pass

    def iteration(self, current: int, maximum: int, task_id: str, title: str) -> None:
        pass

    @contextmanager
    def step(self, icon: str, label: str) -> Iterator[None]:
        yield

    def summary(self, completed: int, total: int, blocked: int, failed: int) -> None:
        pass
