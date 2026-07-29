from __future__ import annotations

import os
import unittest

from ralph_loop.git import Git
from ralph_loop.util import matches_any

from tests.helpers import Repo


class GitTests(unittest.TestCase):
    def test_dot_prefixed_paths_keep_their_leading_dot(self):
        self.assertTrue(matches_any(".ralph/runtime/state.json", [".ralph/**"]))
        self.assertFalse(matches_any("ralph/runtime/state.json", [".ralph/**"]))
        self.assertTrue(matches_any("./src/value.txt", ["src/**"]))

    def test_snapshot_and_candidate_digest_include_file_mode(self):
        repo = Repo()
        self.addCleanup(repo.close)
        path = repo.root / "src" / "tool.sh"
        path.parent.mkdir()
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        git = Git(repo.root)
        before = git.file_snapshot(repo.root)
        digest_before = git.candidate_digest(repo.root, ["src/**"])

        os.chmod(path, 0o755)

        after = git.file_snapshot(repo.root)
        digest_after = git.candidate_digest(repo.root, ["src/**"])
        self.assertEqual(Git.changed_since(before, after), {"src/tool.sh"})
        self.assertNotEqual(digest_before, digest_after)


if __name__ == "__main__":
    unittest.main()
