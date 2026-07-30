#!/usr/bin/env python3
"""Build Ralph as a self-contained Python zip application."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ralph-zipapp-") as directory:
        stage = Path(directory)
        shutil.copytree(
            ROOT / "ralph_loop",
            stage / "ralph_loop",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        zipapp.create_archive(
            stage,
            target=output,
            interpreter="/usr/bin/env python3",
            main="ralph_loop.cli:main",
            compressed=True,
        )
    output.chmod(0o755)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "ralph")
    args = parser.parse_args()
    print(build(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
