"""End-to-end tests for the recipe-cache CLI.

Exercise pack -> list -> get -> rm against a synthesised "install
tree" fixture in a tmp cache dir. Doesn't run `build` because that
shells out to a real recipe (~30 min); pack mode covers the same
cache I/O codepath.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "recipe-cache"


def _have_zstd() -> bool:
    try:
        subprocess.run(
            ["zstd", "--version"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


class _CLITestCase(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp(prefix="rc-")
        self.env = os.environ | {"RECIPE_CACHE_DIR": self.cache_dir}

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def _run(self, *args, expect_rc=0):
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            env=self.env, capture_output=True, text=True,
        )
        if result.returncode != expect_rc:
            raise AssertionError(
                f"recipe-cache {' '.join(args)} exited {result.returncode}, "
                f"expected {expect_rc}\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result


class KeyCommandTests(_CLITestCase):
    def test_outputs_full_key(self):
        # llvm-asan is a real recipe in the tree.
        result = self._run("key", "llvm-asan", "22", "ubuntu-24.04", "x86_64")
        key = result.stdout.strip()
        self.assertTrue(key.startswith("llvm-asan-22-ubuntu-24.04-x86_64-"))
        # Hash suffix is 16 hex chars.
        suffix = key.rsplit("-", 1)[1]
        self.assertEqual(len(suffix), 16)
        int(suffix, 16)


@unittest.skipUnless(_have_zstd(), "zstd not available")
class PackGetRmTests(_CLITestCase):
    def _make_install_tree(self, root: Path) -> None:
        (root / "lib" / "cmake" / "llvm").mkdir(parents=True)
        (root / "lib" / "cmake" / "llvm" / "LLVMConfig.cmake").write_text("fake\n")
        (root / "include" / "llvm").mkdir(parents=True)
        (root / "include" / "llvm" / "foo.h").write_text("hdr\n")

    def test_pack_then_get_roundtrip(self):
        with tempfile.TemporaryDirectory() as src_dir, \
             tempfile.TemporaryDirectory() as out_dir:
            self._make_install_tree(Path(src_dir))

            # Pack a synthetic tree.
            pack = self._run(
                "pack", "llvm-asan", "22", "ubuntu-24.04", "x86_64",
                "--from", src_dir,
            )
            self.assertIn("packed:", pack.stdout)

            # The cache should now hold a .tar.zst + .manifest.json.
            assets = list(Path(self.cache_dir).glob("*.tar.zst"))
            manifests = list(Path(self.cache_dir).glob("*.manifest.json"))
            self.assertEqual(len(assets), 1)
            self.assertEqual(len(manifests), 1)

            # Manifest is well-formed JSON with kind=mockup.
            manifest_data = json.loads(manifests[0].read_text())
            self.assertEqual(manifest_data["kind"], "mockup")
            self.assertEqual(manifest_data["recipe"], "llvm-asan")

            # Get extracts the tarball under --out/llvm-project/.
            self._run(
                "get", "llvm-asan", "22", "ubuntu-24.04", "x86_64",
                "--out", out_dir,
            )
            extracted = Path(out_dir) / "llvm-project"
            self.assertTrue(extracted.is_dir())
            self.assertEqual(
                (extracted / "include" / "llvm" / "foo.h").read_text(),
                "hdr\n",
            )

            # List shows the cached cell with kind=mockup.
            ls = self._run("list")
            self.assertIn("[mockup]", ls.stdout)
            self.assertIn("llvm-asan-22-ubuntu-24.04-x86_64-", ls.stdout)

    def test_get_miss_without_build_on_miss(self):
        # Empty cache → get should fail with rc=1 and print a populate hint.
        result = self._run(
            "get", "llvm-asan", "22", "ubuntu-24.04", "x86_64",
            expect_rc=1,
        )
        self.assertIn("miss:", result.stderr)
        self.assertIn("recipe-cache build", result.stderr)

    def test_rm_unknown_key(self):
        self._run("rm", "no-such-key", expect_rc=2)

    def test_rm_existing_key(self):
        with tempfile.TemporaryDirectory() as src_dir:
            self._make_install_tree(Path(src_dir))
            self._run(
                "pack", "llvm-asan", "22", "ubuntu-24.04", "x86_64",
                "--from", src_dir,
            )
            key = self._run("key",
                            "llvm-asan", "22", "ubuntu-24.04", "x86_64"
                            ).stdout.strip()
            rm = self._run("rm", key)
            self.assertIn("removed:", rm.stdout)
            self.assertFalse((Path(self.cache_dir) / f"{key}.tar.zst").exists())


class ListEmptyTests(_CLITestCase):
    def test_list_empty(self):
        result = self._run("list")
        self.assertIn("empty", result.stdout)


class UnknownCommandTests(_CLITestCase):
    def test_unknown_command_returns_error(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "nope"],
            env=self.env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
