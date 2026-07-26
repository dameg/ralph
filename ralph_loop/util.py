from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace(os.sep, "/").lstrip("./")
    for pattern in patterns:
        candidate = pattern.replace(os.sep, "/").lstrip("./")
        if candidate.endswith("/**"):
            prefix = candidate[:-3].rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
    return False


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "task"


def ensure_string_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label} must be a non-empty-string array")
    return list(value)


def ensure_command(value: Any, label: str) -> List[str]:
    command = ensure_string_list(value, label)
    if not command:
        raise ValueError(f"{label} cannot be empty")
    return command


def stable_env(extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "NO_COLOR": "1",
            "CI": "1",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def unique(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(items))
