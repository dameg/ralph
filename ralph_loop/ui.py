from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from threading import Event, Thread
from typing import Any, Iterator, Mapping, Optional, Sequence


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
        self.interactive = sys.stdout.isatty() and not os.getenv("NO_COLOR")

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

    def prd_started(self, prd: str, current: int, total: int, resumed: bool = False) -> None:
        action = "Resuming" if resumed else "Starting"
        self.info(f"{action} PRD: {prd} · {current}/{total} tasks", "📚")

    def iteration(
        self,
        current: int,
        task_id: str,
        title: str,
        prd: Optional[str] = None,
        prd_position: Optional[tuple[int, int]] = None,
    ) -> None:
        print()
        print(self.paint(f"── Stage {current} · {task_id} ──", "blue"))
        print(f"   {title}")
        if prd:
            position = f" · task {prd_position[0]}/{prd_position[1]}" if prd_position else ""
            print(self.paint(f"   PRD: {prd}{position}", "dim"))

    @contextmanager
    def step(self, icon: str, label: str) -> Iterator[None]:
        started = time.monotonic()
        stop = Event()
        spinner: Optional[Thread] = None
        if self.interactive:
            frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

            def render() -> None:
                index = 0
                while not stop.is_set():
                    elapsed = format_duration(time.monotonic() - started)
                    print(f"\r{frames[index]}  {icon} {label} · {elapsed}\033[K", end="", flush=True)
                    index = (index + 1) % len(frames)
                    stop.wait(0.1)

            spinner = Thread(target=render, daemon=True)
            spinner.start()
        else:
            print(f"{icon}  {label}…", flush=True)
        try:
            yield
        except Exception:
            elapsed = time.monotonic() - started
            self._finish_step(stop, spinner, f"{icon}  {label} · failed ({format_duration(elapsed)})", "red")
            raise
        else:
            elapsed = time.monotonic() - started
            self._finish_step(stop, spinner, f"{icon}  {label} · done ({format_duration(elapsed)})", "green")

    def _finish_step(self, stop: Event, spinner: Optional[Thread], message: str, style: str) -> None:
        stop.set()
        if spinner:
            spinner.join(timeout=0.2)
            print(f"\r{self.paint(message, style)}\033[K")
        else:
            print(f"   {self.paint(message.split(' · ', 1)[-1], style)}")

    def summary(
        self,
        completed: int,
        total: int,
        blocked: int,
        intervention: int,
        active_seconds: float = 0,
        task_usage: Optional[Mapping[str, Any]] = None,
        total_usage: Optional[Mapping[str, Any]] = None,
        prd_rows: Optional[Sequence[tuple[str, int, int, float, Optional[Mapping[str, Any]]]]] = None,
        all_complete: bool = False,
    ) -> None:
        print()
        print(self.paint("━" * 68, "blue"))
        if all_complete:
            print(self.paint("🎉 All PRDs complete", "green"))
        percentage = round(completed * 100 / total) if total else 0
        print(
            f"📊 {completed}/{total} completed · {percentage}%"
            f"  ·  ⛔ {blocked} blocked"
            f"  ·  🛟 {intervention} needs intervention"
        )
        print(f"   {progress_bar(completed, total)}")
        print(f"   ⏱ Active work: {format_duration(active_seconds)}")
        if task_usage is not None:
            print(f"   🪙 This task: {format_usage(task_usage)}")
        if total_usage is not None:
            print(f"   🪙 Total: {format_usage(total_usage)}")
        if prd_rows:
            for prd, done, count, seconds, usage in prd_rows:
                token_text = f" · 🪙 {format_usage(usage)}" if usage is not None else ""
                print(f"   📚 {prd} · {done}/{count} complete · ⏱ {format_duration(seconds)}{token_text}")
        print(self.paint("━" * 68, "blue"))


def format_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def progress_bar(completed: int, total: int, width: int = 28) -> str:
    filled = round(width * completed / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def format_usage(usage: Any) -> str:
    def value(name: str, default: Any = 0) -> Any:
        if isinstance(usage, Mapping):
            return usage.get(name, default)
        return getattr(usage, name, default)

    if not value("available", False):
        return "usage unavailable"
    return (
        f"{format_tokens(int(value('input_tokens')))} input · "
        f"{format_tokens(int(value('output_tokens')))} output · "
        f"{format_tokens(int(value('cached_input_tokens')))} cached"
    )


def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


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

    def prd_started(self, prd: str, current: int, total: int, resumed: bool = False) -> None:
        pass

    def iteration(self, current: int, task_id: str, title: str, **kwargs: Any) -> None:
        pass

    @contextmanager
    def step(self, icon: str, label: str) -> Iterator[None]:
        yield

    def summary(self, completed: int, total: int, blocked: int, intervention: int, **kwargs: Any) -> None:
        pass
