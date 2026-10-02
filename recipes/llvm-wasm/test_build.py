"""Unit tests for the llvm-wasm recipe's emsdk plumbing.

Loaded by path rather than imported: see recipes/llvm-release/test_build.py.
They run on every python-unit-tests leg, Windows included, which is
where the emsdk plumbing differs.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "llvm_wasm_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class EmsdkPlumbingTests(unittest.TestCase):
    """What the Windows port depends on: no shell between build.py and
    cmake, and no ccache in front of emcc.bat."""

    def test_cmake_lists_reach_cmake_unsplit(self):
        with mock.patch.object(subprocess, "run") as run:
            build.run_in_emsdk("emcmake",
                               ["cmake", "-DLLVM_ENABLE_PROJECTS=clang;lld",
                                "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG"],
                               Path("emsdk"), Path("."))
        self.assertEqual(run.call_args.args[0][-2:],
                         ["-DLLVM_ENABLE_PROJECTS=clang;lld",
                          "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG"])

    def test_ccache_launcher_is_dropped_only_on_windows(self):
        launchers = {"CMAKE_C_COMPILER_LAUNCHER": "ccache",
                     "CMAKE_CXX_COMPILER_LAUNCHER": "ccache"}
        with mock.patch.dict(os.environ, launchers):
            for windows in (False, True):
                with mock.patch.object(build, "WINDOWS", windows):
                    env = build.emsdk_env(Path("emsdk"))
                for k in launchers:
                    self.assertEqual(k in env, not windows, (k, windows))


if __name__ == "__main__":
    unittest.main()
