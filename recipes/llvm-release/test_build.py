"""Unit tests for the llvm-release recipe's ref resolution.

Loaded by path rather than imported: recipe directories are not
packages ('llvm-release' is not an identifier), and every recipe's
build script is called build.py, so a plain import would collide
across recipes in sys.modules.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "llvm_release_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class SourceRefTests(unittest.TestCase):
    """A cell's version selects between a moving branch and a fixed tag,
    so getting the split wrong publishes an artifact under a name that
    describes something else."""

    def test_bare_major_tracks_the_release_branch(self):
        self.assertEqual(build._source_ref("22"), "release/22.x")
        self.assertEqual(build._source_ref("23"), "release/23.x")

    def test_dotted_version_pins_the_llvmorg_tag(self):
        self.assertEqual(build._source_ref("23.1.0-rc2"),
                         "llvmorg-23.1.0-rc2")
        self.assertEqual(build._source_ref("22.1.8"), "llvmorg-22.1.8")

    def test_multi_digit_major_is_not_a_tag(self):
        # '\d+' must match the whole string: a two-digit major is still
        # a branch, and nothing but digits may take that path.
        self.assertEqual(build._source_ref("100"), "release/100.x")
        self.assertEqual(build._source_ref("23rc2"), "llvmorg-23rc2")


class RecordSrcRefTests(unittest.TestCase):
    def test_appends_to_github_env(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / "env"
            env_file.write_text("SRC_COMMIT=abc123\n")
            with mock.patch.dict(os.environ,
                                 {"GITHUB_ENV": str(env_file)}, clear=True):
                build._record_src_ref("llvmorg-23.1.0-rc2")
            self.assertEqual(
                env_file.read_text(),
                "SRC_COMMIT=abc123\nSRC_REF=llvmorg-23.1.0-rc2\n",
            )

    def test_no_github_env_is_a_noop(self):
        # Local runs (bin/repro, a developer shell) have no GITHUB_ENV;
        # the build must not fail for want of a place to record.
        with mock.patch.dict(os.environ, {}, clear=True):
            build._record_src_ref("release/22.x")


if __name__ == "__main__":
    unittest.main()
