from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import __version__
from .errors import UpdateError


DEFAULT_RELEASE_BASE = "https://github.com/dameg/ralph/releases"
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
DOWNLOAD_TIMEOUT = 30


@dataclass(frozen=True)
class UpdateResult:
    current: str
    target: str
    changed: bool
    check_only: bool


def normalized_version(value: str) -> str:
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise UpdateError("Version must use stable MAJOR.MINOR.PATCH format")
    return ".".join(match.groups())


def version_key(value: str) -> Tuple[int, int, int]:
    normalized = normalized_version(value)
    major, minor, patch = normalized.split(".")
    return int(major), int(minor), int(patch)


def release_base_url() -> str:
    return os.environ.get("RALPH_RELEASE_BASE_URL", DEFAULT_RELEASE_BASE).rstrip("/")


def _request(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": f"ralph/{__version__}"})
    try:
        return urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT)
    except (OSError, urllib.error.URLError) as error:
        raise UpdateError(f"Cannot download {url}: {error}") from error


def latest_version(base_url: Optional[str] = None) -> str:
    url = f"{(base_url or release_base_url()).rstrip('/')}/latest"
    try:
        with _request(url) as response:
            final_url = response.geturl()
    except UpdateError:
        raise
    tag = urllib.parse.unquote(urllib.parse.urlparse(final_url).path.rstrip("/").split("/")[-1])
    try:
        return normalized_version(tag)
    except UpdateError as error:
        raise UpdateError(f"Latest release redirected to an invalid tag: {tag}") from error


def _download(url: str, target: Path) -> None:
    try:
        with _request(url) as response, target.open("wb") as handle:
            shutil.copyfileobj(response, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError(f"Cannot write downloaded update: {error}") from error


def _expected_checksum(path: Path) -> str:
    try:
        fields = path.read_text(encoding="utf-8").strip().split()
    except OSError as error:
        raise UpdateError(f"Cannot read update checksum: {error}") from error
    if not fields or not CHECKSUM_PATTERN.fullmatch(fields[0]):
        raise UpdateError("Release checksum is invalid")
    return fields[0].lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise UpdateError(f"Cannot read downloaded update: {error}") from error
    return digest.hexdigest()


def _validate_artifact(path: Path, version: str) -> None:
    if not zipfile.is_zipfile(path):
        raise UpdateError("Downloaded Ralph artifact is not a valid zipapp")
    try:
        result = subprocess.run(
            [sys.executable, str(path), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"Cannot validate downloaded Ralph artifact: {error}") from error
    expected = f"ralph {version}"
    if result.returncode != 0 or result.stdout.strip() != expected:
        detail = result.stderr.strip() or result.stdout.strip() or "no version output"
        raise UpdateError(f"Downloaded Ralph failed validation (expected {expected!r}): {detail}")


def installed_executable(value: Optional[Path] = None) -> Path:
    if value is None:
        supplied = Path(sys.argv[0])
        if supplied.is_absolute():
            candidate = supplied
        else:
            located = shutil.which(str(supplied))
            candidate = Path(located) if located else supplied
    else:
        candidate = value
    candidate = Path(os.path.abspath(str(candidate)))
    if candidate.is_symlink():
        raise UpdateError(
            "Ralph update does not replace symlinks; reinstall with the release install.sh"
        )
    if not candidate.is_file() or not zipfile.is_zipfile(candidate):
        raise UpdateError(
            "Ralph update is available only for the installed zipapp; "
            "reinstall with the release install.sh"
        )
    return candidate


def perform_update(
    *,
    check_only: bool = False,
    requested_version: Optional[str] = None,
    force: bool = False,
    executable: Optional[Path] = None,
    current_version: str = __version__,
    base_url: Optional[str] = None,
) -> UpdateResult:
    target_path = installed_executable(executable)
    current = normalized_version(current_version)
    base = (base_url or release_base_url()).rstrip("/")
    target = normalized_version(requested_version) if requested_version else latest_version(base)

    if check_only:
        return UpdateResult(current=current, target=target, changed=False, check_only=True)
    if version_key(target) < version_key(current) and not force:
        raise UpdateError(
            f"Refusing to downgrade Ralph {current} to {target} without --force"
        )
    if target == current and not force:
        return UpdateResult(current=current, target=target, changed=False, check_only=False)

    tag = f"v{target}"
    asset_url = f"{base}/download/{tag}/ralph"
    checksum_url = f"{asset_url}.sha256"
    temporary_dir: Optional[Path] = None
    try:
        temporary_dir = Path(tempfile.mkdtemp(prefix=".ralph-update-", dir=str(target_path.parent)))
        artifact = temporary_dir / "ralph"
        checksum = temporary_dir / "ralph.sha256"
        _download(asset_url, artifact)
        _download(checksum_url, checksum)
        expected = _expected_checksum(checksum)
        actual = _sha256(artifact)
        if actual != expected:
            raise UpdateError(
                f"Ralph update checksum mismatch (expected {expected}, received {actual})"
            )
        _validate_artifact(artifact, target)
        artifact.chmod(0o755)
        os.replace(str(artifact), str(target_path))
    except UpdateError:
        raise
    except OSError as error:
        raise UpdateError(f"Cannot replace {target_path}: {error}") from error
    finally:
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
    return UpdateResult(current=current, target=target, changed=True, check_only=False)
