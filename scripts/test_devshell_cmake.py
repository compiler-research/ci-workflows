"""Unit tests for the devshell cmake-argument replay.

The rewrite is what keeps a devshell's compile commands identical to the
producer's, so a regression here does not fail loudly -- it silently turns every
ccache hit into a miss, or configures against a path that does not exist.
"""

from __future__ import annotations

import unittest

import devshell_cmake

LOCAL = {"prefix": "/local/install", "src": "/local/src",
         "build": "/local/build"}


class LlvmShapeTests(unittest.TestCase):
    """LLVM runs cmake from the build directory and passes a relative
    ../llvm. This is the shape the devshell supported before it had to serve
    anything else, so it must come through unchanged."""

    def test_relative_llvm_source_resolves_under_the_local_tree(self):
        out = devshell_cmake.rewrite(
            ["cmake", "-DCMAKE_INSTALL_PREFIX=/prod/out",
             "-DLLVM_ENABLE_PROJECTS=clang", "../llvm"], **LOCAL)
        self.assertEqual(out, ["-DCMAKE_INSTALL_PREFIX=/local/install",
                               "-DLLVM_ENABLE_PROJECTS=clang",
                               "/local/src/llvm"])

    def test_leading_cmake_is_dropped(self):
        self.assertNotIn("cmake", devshell_cmake.rewrite(["cmake"], **LOCAL))


class SeparateSourceBuildShapeTests(unittest.TestCase):
    """Every non-LLVM recipe passes absolute -S/-B naming the producer's
    filesystem. Passing those through unchanged configures against nothing --
    the bug this rewrite exists to fix."""

    def test_separate_form_is_rewritten(self):
        out = devshell_cmake.rewrite(
            ["cmake", "-S", "/prod/work/biodynamo", "-B", "/prod/work/build",
             "-DNOPYENV=YES", "-DCMAKE_INSTALL_PREFIX=/prod/staging"], **LOCAL)
        self.assertEqual(out, ["-S", "/local/src", "-B", "/local/build",
                               "-DNOPYENV=YES",
                               "-DCMAKE_INSTALL_PREFIX=/local/install"])

    def test_joined_form_is_rewritten(self):
        out = devshell_cmake.rewrite(
            ["cmake", "-S/prod/src", "-B/prod/build"], **LOCAL)
        self.assertEqual(out, ["-S/local/src", "-B/local/build"])

    def test_producer_paths_never_survive(self):
        """The point of the rewrite: no /prod path may reach the output."""
        out = devshell_cmake.rewrite(
            ["cmake", "-S", "/prod/src", "-B", "/prod/build",
             "-DCMAKE_INSTALL_PREFIX=/prod/install"], **LOCAL)
        self.assertFalse([a for a in out if "/prod" in a], out)


class PassthroughTests(unittest.TestCase):
    def test_unrelated_flags_are_untouched(self):
        flags = ["-DCMAKE_BUILD_TYPE=Release", "-GNinja",
                 "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache"]
        self.assertEqual(devshell_cmake.rewrite(["cmake", *flags], **LOCAL),
                         flags)

    def test_a_flag_whose_value_merely_starts_with_dash_s_is_not_a_path(self):
        """-Sfoo is a source dir, but -DSOMETHING is not -- guard the prefix
        match against eating ordinary defines."""
        out = devshell_cmake.rewrite(["cmake", "-DSANITIZE=address"], **LOCAL)
        self.assertEqual(out, ["-DSANITIZE=address"])

    def test_empty_args_yield_empty_output(self):
        self.assertEqual(devshell_cmake.rewrite([], **LOCAL), [])


if __name__ == "__main__":
    unittest.main()
