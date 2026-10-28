"""Unit tests for the AI links devshell-init lays down (--link-ai).

A broken link here does not fail loudly: claude just starts without its
memory, or writes its transcripts into the container's own layer, where
the next re-created container loses them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INIT = Path(__file__).resolve().parent / "devshell-init"
CWDS = ("-patches", "-home-runner-work-x-x")


@unittest.skipIf(sys.platform == "win32" or not shutil.which("bash"),
                 "needs bash and symlinks")
class LinkAiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / "cache"
        self.home = self.root / "home"
        (self.cache / "ai" / "skills").mkdir(parents=True)
        self.home.mkdir()
        self.mem = self.cache / "ai" / "memory" / "clad" / "-Users-me-clad"
        self.tr = self.cache / "ai" / "transcripts" / "clad" / "-Users-me-clad"

    def _link(self, cache=None):
        env = dict(os.environ,
                   DEVSHELL_CACHE_MOUNT=str(cache or self.cache),
                   DEVSHELL_DEV_HOME=str(self.home),
                   DEVSHELL_PATCHES_MOUNT="/patches",
                   DEVSHELL_BUILD="/home/runner/work/x/x/_recipe_work/b",
                   DEVSHELL_AI_REPO="clad",
                   DEVSHELL_AI_REPO_ENCODED="-Users-me-clad")
        subprocess.run(["bash", str(INIT), "--link-ai"], env=env, check=True)

    def _project(self, enc: str) -> Path:
        return self.home / ".claude" / "projects" / enc

    def test_each_cwd_keeps_its_transcripts_in_the_host_cache(self):
        self._link()
        for enc in CWDS:
            self.assertEqual(os.readlink(self._project(enc)),
                             str(self.tr / enc))
        session = self._project("-patches") / "s.jsonl"
        session.write_text("{}")
        self.assertTrue((self.tr / "-patches" / "s.jsonl").is_file())

    def test_memory_resolves_inside_each_project_dir(self):
        self._link()
        (self.mem / "MEMORY.md").write_text("note")
        for enc in CWDS:
            self.assertEqual(
                (self._project(enc) / "memory" / "MEMORY.md").read_text(),
                "note")
        self.assertEqual(os.readlink(self.home / ".claude" / "skills"),
                         str(self.cache / "ai" / "skills"))

    def test_rerunning_changes_nothing(self):
        self._link()
        before = sorted(str(p.relative_to(self.root))
                        for p in self.root.rglob("*"))
        self._link()
        after = sorted(str(p.relative_to(self.root))
                       for p in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_without_a_host_cache_nothing_is_linked(self):
        self._link(cache=self.root / "absent")
        self.assertFalse((self.home / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
