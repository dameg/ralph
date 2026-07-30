from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path
from typing import Optional
from unittest import mock

from ralph_loop.errors import UpdateError
from ralph_loop.updater import latest_version, perform_update
from scripts.build_zipapp import build


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    environment = dict(os.environ)
    environment["PYTHONPYCACHEPREFIX"] = "/tmp/ralph-distribution-pycache"
    environment.update(kwargs.pop("env", {}))
    return subprocess.run(
        [str(value) for value in command],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **kwargs,
    )


def fake_zipapp(path: Path, version: str) -> Path:
    stage = path.parent / f"stage-{version}"
    stage.mkdir(parents=True)
    (stage / "__main__.py").write_text(
        "import sys\n"
        f"print('ralph {version}' if '--version' in sys.argv else 'fake Ralph')\n",
        encoding="utf-8",
    )
    marker = stage / "ralph_loop" / "zipapp-marker.txt"
    marker.parent.mkdir()
    marker.write_text("ralph-zipapp-v1\n", encoding="utf-8")
    zipapp.create_archive(stage, path, interpreter="/usr/bin/env python3", compressed=True)
    path.chmod(0o755)
    shutil.rmtree(stage)
    return path


def publish(
    root: Path, version: str, artifact: Path, checksum: Optional[str] = None
) -> None:
    release = root / "releases" / "download" / f"v{version}"
    release.mkdir(parents=True, exist_ok=True)
    target = release / "ralph"
    shutil.copy2(artifact, target)
    digest = checksum or hashlib.sha256(target.read_bytes()).hexdigest()
    (release / "ralph.sha256").write_text(f"{digest}  ralph\n", encoding="utf-8")


def publish_latest(root: Path, artifact: Path) -> None:
    latest = root / "releases" / "latest" / "download"
    latest.mkdir(parents=True, exist_ok=True)
    target = latest / "ralph"
    shutil.copy2(artifact, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (latest / "ralph.sha256").write_text(f"{digest}  ralph\n", encoding="utf-8")


class ZipappTests(unittest.TestCase):
    def test_built_zipapp_runs_and_copies_packaged_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = build(root / "ralph")
            version = run([artifact, "--version"])
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), "ralph 1.0.0")
            help_result = run([artifact, "--help"])
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("update", help_result.stdout)

            repository = root / "project"
            repository.mkdir()
            initialized_git = run(["git", "init", "-b", "main"], cwd=repository)
            self.assertEqual(initialized_git.returncode, 0, initialized_git.stderr)
            initialized = run([artifact, "init"], cwd=repository)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertTrue((repository / ".ralph/prompts/planner.md").is_file())
            self.assertTrue((repository / ".ralph/schemas/reviewer-result.schema.json").is_file())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        artifact = build(self.root / "release-ralph")
        publish(self.root, "1.0.0", artifact)
        publish_latest(self.root, artifact)
        self.bin_dir = self.root / "bin with spaces"
        self.environment = {
            "RALPH_RELEASE_BASE_URL": (self.root / "releases").as_uri(),
            "PATH": os.environ.get("PATH", ""),
        }

    def install(self, *arguments):
        return run(
            ["sh", SOURCE_ROOT / "install.sh", "--bin-dir", self.bin_dir, *arguments],
            env=self.environment,
        )

    def test_install_reinstall_latest_and_uninstall(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        target = self.bin_dir / "ralph"
        self.assertEqual(run([target, "--version"]).stdout.strip(), "ralph 1.0.0")

        second = self.install("--version", "v1.0.0")
        self.assertEqual(second.returncode, 0, second.stderr)
        removed = self.install("--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(target.exists())

    def test_default_location_uses_home_local_bin(self):
        home = self.root / "home"
        result = run(
            ["sh", SOURCE_ROOT / "install.sh", "--version", "1.0.0"],
            env={**self.environment, "HOME": str(home)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        target = home / ".local/bin/ralph"
        self.assertEqual(run([target, "--version"]).stdout.strip(), "ralph 1.0.0")

    def test_foreign_target_is_preserved_without_force(self):
        self.bin_dir.mkdir(parents=True)
        target = self.bin_dir / "ralph"
        target.write_text("foreign\n", encoding="utf-8")
        result = self.install("--version", "1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "foreign\n")

    def test_bad_checksum_does_not_install(self):
        checksum = self.root / "releases/download/v1.0.0/ralph.sha256"
        checksum.write_text(f"{'0' * 64}  ralph\n", encoding="utf-8")
        result = self.install("--version", "1.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse((self.bin_dir / "ralph").exists())


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = fake_zipapp(self.root / "ralph", "1.0.0")
        release = fake_zipapp(self.root / "ralph-1.1.0", "1.1.0")
        publish(self.root, "1.1.0", release)
        old_release = fake_zipapp(self.root / "ralph-0.9.0", "0.9.0")
        publish(self.root, "0.9.0", old_release)
        self.base_url = (self.root / "releases").as_uri()

    def test_check_and_successful_update(self):
        before = self.current.read_bytes()
        with mock.patch("ralph_loop.updater.latest_version", return_value="1.1.0"):
            checked = perform_update(
                check_only=True,
                executable=self.current,
                current_version="1.0.0",
                base_url=self.base_url,
            )
        self.assertEqual(checked.target, "1.1.0")
        self.assertFalse(checked.changed)
        self.assertEqual(self.current.read_bytes(), before)

        with mock.patch("ralph_loop.updater.latest_version", return_value="1.1.0"):
            updated = perform_update(
                executable=self.current,
                current_version="1.0.0",
                base_url=self.base_url,
            )
        self.assertTrue(updated.changed)
        self.assertEqual(run([self.current, "--version"]).stdout.strip(), "ralph 1.1.0")

    def test_latest_release_version_is_read_from_redirect_url(self):
        response = mock.MagicMock()
        response.__enter__.return_value.geturl.return_value = (
            "https://github.com/dameg/ralph/releases/tag/v1.2.3"
        )
        with mock.patch("ralph_loop.updater._request", return_value=response):
            self.assertEqual(latest_version(), "1.2.3")

    def test_downgrade_requires_force(self):
        with self.assertRaisesRegex(UpdateError, "Refusing to downgrade"):
            perform_update(
                requested_version="0.9.0",
                executable=self.current,
                current_version="1.0.0",
                base_url=self.base_url,
            )
        downgraded = perform_update(
            requested_version="0.9.0",
            force=True,
            executable=self.current,
            current_version="1.0.0",
            base_url=self.base_url,
        )
        self.assertTrue(downgraded.changed)
        self.assertEqual(run([self.current, "--version"]).stdout.strip(), "ralph 0.9.0")

    def test_checksum_failure_preserves_installed_executable(self):
        checksum = self.root / "releases/download/v1.1.0/ralph.sha256"
        checksum.write_text(f"{'f' * 64}  ralph\n", encoding="utf-8")
        before = self.current.read_bytes()
        with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
            with mock.patch("ralph_loop.updater.latest_version", return_value="1.1.0"):
                perform_update(
                    executable=self.current,
                    current_version="1.0.0",
                    base_url=self.base_url,
                )
        self.assertEqual(self.current.read_bytes(), before)

    def test_update_from_checkout_is_rejected_before_repository_discovery(self):
        result = run([sys.executable, "-m", "ralph_loop", "update", "--check"], cwd=SOURCE_ROOT)
        self.assertEqual(result.returncode, 1)
        self.assertIn("installed zipapp", result.stderr)


if __name__ == "__main__":
    unittest.main()
