"""Unit tests for the biodynamo recipe's install-tree flattening.

Loaded by path rather than imported: recipe directories are not packages
('llvm-release' is not an identifier), and every recipe's build script is
called build.py, so a plain import would collide across recipes in
sys.modules. Mirrors recipes/llvm-release/test_build.py.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "biodynamo_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class FlattenInstallTests(unittest.TestCase):
    """BioDynaMo installs into <prefix>/biodynamo-v<X>/ while setup-recipe
    expects the tarball root to be `install/`. Getting this wrong publishes an
    artifact whose layout no consumer can source, which only shows up on the
    consumer side long after the cell is warm."""

    def test_moves_the_single_versioned_dir_to_dest(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            (staging / "biodynamo-v1.05" / "bin").mkdir(parents=True)
            (staging / "biodynamo-v1.05" / "bin" / "thisbdm.sh").write_text("x")
            dest = Path(tmp) / "install"

            out = build._flatten_install(staging, dest)

            self.assertEqual(out, dest)
            self.assertTrue((dest / "bin" / "thisbdm.sh").is_file())
            self.assertFalse(staging.exists())

    def test_replaces_an_existing_dest(self):
        """A re-run in a reused workspace must not merge into the old tree."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            (staging / "biodynamo-v1.05").mkdir(parents=True)
            (staging / "biodynamo-v1.05" / "new").write_text("new")
            dest = Path(tmp) / "install"
            dest.mkdir()
            (dest / "stale").write_text("stale")

            build._flatten_install(staging, dest)

            self.assertTrue((dest / "new").is_file())
            self.assertFalse((dest / "stale").exists())

    def test_rejects_more_than_one_candidate(self):
        """Two directories means the install layout changed upstream; guessing
        would silently publish half a tree."""
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            (staging / "biodynamo-v1.05").mkdir(parents=True)
            (staging / "biodynamo-v1.06").mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                build._flatten_install(staging, Path(tmp) / "install")

    def test_rejects_an_empty_staging_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / "staging"
            staging.mkdir()
            with self.assertRaises(RuntimeError):
                build._flatten_install(staging, Path(tmp) / "install")


class SudoGuardTests(unittest.TestCase):
    """The recipe shells out to apt, so it is POSIX-only by construction; the
    guard exists so a Windows invocation says that rather than dying inside os."""

    def test_non_posix_host_gets_a_reason(self):
        import os as _os
        had = hasattr(_os, "geteuid")
        if not had:
            with self.assertRaises(RuntimeError):
                build._sudo()
            return
        geteuid = _os.geteuid
        del _os.geteuid
        try:
            with self.assertRaises(RuntimeError):
                build._sudo()
        finally:
            _os.geteuid = geteuid


if __name__ == "__main__":
    unittest.main()
