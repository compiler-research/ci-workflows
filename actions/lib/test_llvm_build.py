"""Unit tests for llvm_build.

Covers env validation, cmake_extra plumbing, intermediate cleanup,
and the install_distribution component-list assembly. The orchestration
parts that shell out to cmake/ninja (run_install_distribution, smoke)
are exercised end-to-end by verify.yml's publish-dryrun matrix
against a real LLVM tree — a unit-level test there would just be
re-implementing subprocess mocks.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llvm_build


class SetupEnvTests(unittest.TestCase):
    def test_missing_required_env_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EnvironmentError):
                llvm_build.setup_env()

    def test_creates_dirs_and_sets_ncpus(self):
        with tempfile.TemporaryDirectory() as d:
            env = {
                "RECIPE_VERSION": "22",
                "WORK_DIR": str(Path(d) / "work"),
                "OUT_DIR": str(Path(d) / "out"),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                llvm_build.setup_env()
                self.assertTrue(Path(env["WORK_DIR"]).is_dir())
                self.assertTrue(Path(env["OUT_DIR"]).is_dir())
                self.assertGreaterEqual(int(os.environ["NCPUS"]), 1)

    def test_existing_ncpus_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            env = {
                "RECIPE_VERSION": "22",
                "WORK_DIR": d,
                "OUT_DIR": d,
                "NCPUS": "7",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                llvm_build.setup_env()
                self.assertEqual(os.environ["NCPUS"], "7")


class CmakeExtraTests(unittest.TestCase):
    def test_empty_when_no_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llvm_build.cmake_extra(), [])

    def test_emits_each_set_var(self):
        env = {
            "CMAKE_C_COMPILER_LAUNCHER": "ccache",
            "CMAKE_CXX_COMPILER":        "/usr/bin/clang++",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                set(llvm_build.cmake_extra()),
                {
                    "-DCMAKE_C_COMPILER_LAUNCHER=ccache",
                    "-DCMAKE_CXX_COMPILER=/usr/bin/clang++",
                },
            )

    def test_skips_empty_string(self):
        env = {"CMAKE_C_COMPILER_LAUNCHER": ""}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(llvm_build.cmake_extra(), [])


class CleanupIntermediatesTests(unittest.TestCase):
    def test_removes_o_and_obj_recursively(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "a" / "x.o").write_bytes(b"")
            (root / "a" / "y.obj").write_bytes(b"")
            (root / "a" / "z.cpp").write_text("kept")
            (root / "deep" / "nest").mkdir(parents=True)
            (root / "deep" / "nest" / "w.o").write_bytes(b"")

            cwd = os.getcwd()
            try:
                os.chdir(root)
                llvm_build.cleanup_intermediates()
            finally:
                os.chdir(cwd)

            self.assertFalse((root / "a" / "x.o").exists())
            self.assertFalse((root / "a" / "y.obj").exists())
            self.assertFalse((root / "deep" / "nest" / "w.o").exists())
            self.assertEqual(
                (root / "a" / "z.cpp").read_text(), "kept"
            )


class RunInstallDistributionTests(unittest.TestCase):
    """run_install_distribution must invoke `ninja install-<comp>` per
    component — not `cmake --install --component`. The latter runs the
    install rule with no build dependency, so a component whose file
    isn't already on disk (llvm-config in the typical real recipe)
    fails the install step. Per-component ninja install-X builds the
    component first, then runs its install rule."""

    def test_invokes_ninja_install_per_component(self):
        captured = []

        def fake_run(cmd, **_kwargs):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(llvm_build.subprocess, "run", side_effect=fake_run), \
             mock.patch.dict(os.environ, {"NCPUS": "4"}, clear=False):
            llvm_build.run_install_distribution("LLVMSupport;llvm-headers;llvm-config")

        # First call is the cmake reconfigure.
        self.assertEqual(captured[0][0], "cmake")
        self.assertIn("-DLLVM_DISTRIBUTION_COMPONENTS=LLVMSupport;llvm-headers;llvm-config",
                      captured[0])

        # Subsequent calls are ninja install-<comp>, one per component.
        ninja_calls = [c for c in captured if c[0] == "ninja"]
        self.assertEqual(len(ninja_calls), 3)
        targets = [c[-1] for c in ninja_calls]
        self.assertEqual(targets, [
            "install-LLVMSupport",
            "install-llvm-headers",
            "install-llvm-config",
        ])

        # No cmake --install --component calls — that was the bug.
        for c in captured:
            self.assertNotIn("--install", c)

    def test_skips_empty_components(self):
        captured = []

        def fake_run(cmd, **_kwargs):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        with mock.patch.object(llvm_build.subprocess, "run", side_effect=fake_run):
            # Trailing/leading/double semicolons should not produce
            # `ninja install-` (empty target name).
            llvm_build.run_install_distribution(";LLVMSupport;;")

        ninja_calls = [c for c in captured if c[0] == "ninja"]
        self.assertEqual(len(ninja_calls), 1)
        self.assertEqual(ninja_calls[0][-1], "install-LLVMSupport")


class InstallDistributionListTests(unittest.TestCase):
    """install_distribution's component-list assembly (without invoking cmake)."""

    def _capture_dist_str(self):
        captured = {"value": None}
        def fake(dist_str):
            captured["value"] = dist_str
        return captured, fake

    def test_walks_lib_directory(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d)
            (cwd / "lib").mkdir()
            (cwd / "lib" / "libLLVMSupport.a").write_bytes(b"")
            (cwd / "lib" / "libLLVMCore.a").write_bytes(b"")
            (cwd / "lib" / "libclangAST.a").write_bytes(b"")
            (cwd / "lib" / "clangFoo.lib").write_bytes(b"")  # Windows convention
            (cwd / "lib" / "irrelevant.txt").write_bytes(b"")

            captured, fake = self._capture_dist_str()
            saved = os.getcwd()
            try:
                os.chdir(cwd)
                with mock.patch.object(llvm_build, "run_install_distribution", fake):
                    llvm_build.install_distribution()
            finally:
                os.chdir(saved)

            parts = captured["value"].split(";")
            self.assertIn("LLVMSupport", parts)
            self.assertIn("LLVMCore",    parts)
            self.assertIn("clangAST",    parts)
            self.assertIn("clangFoo",    parts)
            # Umbrellas always present.
            self.assertIn("clang",                parts)
            self.assertIn("clang-cmake-exports",  parts)
            self.assertIn("cmake-exports",        parts)

    def test_extras_appended(self):
        with tempfile.TemporaryDirectory() as d:
            captured, fake = self._capture_dist_str()
            saved = os.getcwd()
            try:
                os.chdir(d)
                with mock.patch.object(llvm_build, "run_install_distribution", fake):
                    llvm_build.install_distribution(extras=["orc_rt_osx",
                                                            "orc_rt_iossim"])
            finally:
                os.chdir(saved)

            parts = captured["value"].split(";")
            self.assertIn("orc_rt_osx",    parts)
            self.assertIn("orc_rt_iossim", parts)


if __name__ == "__main__":
    unittest.main()
