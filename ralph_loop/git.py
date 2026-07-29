from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .errors import GitError, ScopeError
from .util import matches_any, sha256_file, stable_env


class Git:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=str(cwd or self.root),
                env=stable_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise GitError(f"Cannot run Git: {error}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(arguments)}: {detail}")
        return result

    def ensure_repository(self) -> None:
        result = self.run(["rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            raise GitError("Ralph must run inside a Git repository")
        actual = Path(result.stdout.strip()).resolve()
        if actual != self.root:
            raise GitError(f"Run Ralph from the repository root: {actual}")

    def ensure_clean(self) -> None:
        paths = self.changed_paths()
        if paths:
            preview = ", ".join(paths[:5])
            suffix = "…" if len(paths) > 5 else ""
            raise GitError(f"The primary worktree is not clean: {preview}{suffix}")

    def ensure_head(self) -> None:
        result = self.run(["rev-parse", "--verify", "HEAD"], check=False)
        if result.returncode != 0:
            raise GitError("The repository does not have an initial commit")

    def ensure_identity(self) -> None:
        result = self.run(["var", "GIT_AUTHOR_IDENT"], check=False)
        if result.returncode != 0:
            raise GitError(
                "Git author identity is not configured (user.name/user.email); cannot create commits"
            )

    def exclude_runtime(self, runtime_relative: str) -> None:
        git_dir = self.run(["rev-parse", "--git-dir"]).stdout.strip()
        git_path = (self.root / git_dir).resolve() if not os.path.isabs(git_dir) else Path(git_dir)
        exclude = git_path / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        rule = f"/{runtime_relative.strip('/')}/"
        existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if rule not in existing:
            with exclude.open("a", encoding="utf-8") as handle:
                if existing and exclude.stat().st_size:
                    handle.write("\n" if not exclude.read_text(encoding="utf-8").endswith("\n") else "")
                handle.write(rule + "\n")

    def head(self, cwd: Optional[Path] = None) -> str:
        return self.run(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()

    def current_branch(self, cwd: Optional[Path] = None) -> str:
        branch = self.run(["branch", "--show-current"], cwd=cwd).stdout.strip()
        if not branch:
            raise GitError("Detached HEAD is not supported")
        return branch

    def branch_exists(self, branch: str) -> bool:
        return self.run(["show-ref", "--verify", f"refs/heads/{branch}"], check=False).returncode == 0

    def branch_head(self, branch: str) -> str:
        return self.run(["rev-parse", "--verify", f"refs/heads/{branch}"]).stdout.strip()

    def ensure_branch_name(self, branch: str) -> None:
        result = self.run(["check-ref-format", "--branch", branch], check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"Invalid Git branch name {branch!r}: {detail}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self.run(
            ["merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        if result.returncode not in {0, 1}:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"Cannot compare commits {ancestor} and {descendant}: {detail}")
        return result.returncode == 0

    def add_worktree(self, path: Path, branch: str, base_sha: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run(["worktree", "add", "-b", branch, str(path), base_sha])

    def remove_worktree(self, path: Path) -> None:
        result = self.run(["worktree", "remove", str(path)], check=False)
        if result.returncode != 0 and path.exists():
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"Failed to remove worktree {path}: {detail}")

    def delete_branch(self, branch: str) -> None:
        self.run(["branch", "-d", branch])

    def changed_paths(self, cwd: Optional[Path] = None) -> List[str]:
        result = self.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=cwd
        )
        paths: List[str] = []
        entries = result.stdout.split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            status = entry[:2]
            path = entry[3:]
            if status[0] in {"R", "C"}:
                # In -z format, the next field is the source path. Both paths matter.
                paths.append(path)
                if index < len(entries) and entries[index]:
                    paths.append(entries[index])
                    index += 1
            else:
                paths.append(path)
        return sorted(set(paths))

    def file_snapshot(self, cwd: Path) -> Dict[str, str]:
        snapshot: Dict[str, str] = {}
        for path in self.changed_paths(cwd):
            absolute = cwd / path
            if absolute.is_symlink():
                snapshot[path] = "symlink:" + os.readlink(absolute)
            elif absolute.is_file():
                mode = oct(absolute.stat().st_mode & 0o7777)
                snapshot[path] = f"file:{mode}:{sha256_file(absolute)}"
            elif absolute.is_dir():
                result = self.run(["rev-parse", "HEAD"], cwd=absolute, check=False)
                head = result.stdout.strip() if result.returncode == 0 else "unknown"
                mode = oct(absolute.stat().st_mode & 0o7777)
                snapshot[path] = f"directory:{mode}:{head}"
            else:
                snapshot[path] = "<missing>"
        return snapshot

    def candidate_digest(self, cwd: Path, allowed: Iterable[str]) -> str:
        digest = hashlib.sha256()
        for path in self.changed_paths(cwd):
            if not matches_any(path, allowed):
                continue
            absolute = cwd / path
            if absolute.is_symlink():
                kind = "symlink"
                mode = "symlink"
                content = os.readlink(absolute)
            elif absolute.is_file():
                kind = "file"
                mode = oct(absolute.stat().st_mode & 0o7777)
                content = sha256_file(absolute)
            elif absolute.is_dir():
                kind = "directory"
                mode = oct(absolute.stat().st_mode & 0o7777)
                result = self.run(["rev-parse", "HEAD"], cwd=absolute, check=False)
                content = result.stdout.strip() if result.returncode == 0 else "unknown"
            else:
                kind = "missing"
                mode = "missing"
                content = "missing"
            digest.update(f"{path}\0{kind}\0{mode}\0{content}\n".encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def changed_since(before: Dict[str, str], after: Dict[str, str]) -> Set[str]:
        return {
            path
            for path in set(before) | set(after)
            if before.get(path, "<clean>") != after.get(path, "<clean>")
        }

    @staticmethod
    def assert_scope(
        paths: Iterable[str],
        allowed: Iterable[str],
        role: str,
        forbidden: Iterable[str] = (),
    ) -> None:
        patterns = list(allowed)
        forbidden_patterns = list(forbidden)
        violations = sorted(
            path
            for path in paths
            if not matches_any(path, patterns) or matches_any(path, forbidden_patterns)
        )
        if violations:
            raise ScopeError(
                f"Role {role} modified files outside its scope: {', '.join(violations)}"
            )

    def commit_all(self, cwd: Path, message: str) -> str:
        if not self.changed_paths(cwd):
            raise GitError("There are no changes to commit")
        self.run(["add", "-A", "--", ":/"], cwd=cwd)
        self.run(["commit", "-m", message], cwd=cwd)
        return self.head(cwd)

    def commit_parent(self, commit: str, cwd: Optional[Path] = None) -> str:
        fields = self.run(
            ["rev-list", "--parents", "-n", "1", commit], cwd=cwd
        ).stdout.split()
        parents = fields[1:]
        if len(parents) != 1:
            raise GitError(
                f"Final task commit {commit} must have exactly one parent; found {len(parents)}"
            )
        return parents[0]

    def committed_paths(
        self, base: str, commit: str, cwd: Optional[Path] = None
    ) -> List[str]:
        result = self.run(
            ["diff", "--name-only", "--no-renames", "-z", base, commit], cwd=cwd
        )
        return sorted(path for path in result.stdout.split("\0") if path)

    def fast_forward(
        self, expected_base: str, branch: str, expected_branch: Optional[str] = None
    ) -> str:
        self.ensure_clean()
        if expected_branch and self.current_branch() != expected_branch:
            raise GitError(
                f"The active branch is {self.current_branch()}; expected {expected_branch}"
            )
        actual = self.head()
        if actual != expected_base:
            raise GitError(
                "The primary branch changed while the task was running; preserving the isolated task branch"
            )
        self.run(["merge", "--ff-only", branch])
        return self.head()
