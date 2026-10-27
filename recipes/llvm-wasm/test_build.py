"""Unit tests for the llvm-wasm recipe's emsdk plumbing.

Loaded by path rather than imported: see recipes/llvm-release/test_build.py.
They run on every python-unit-tests leg, Windows included, which is
where the emsdk plumbing differs.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "llvm_wasm_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class EmsdkPlumbingTests(unittest.TestCase):
    """What the Windows port depends on: no shell between build.py and
    cmake, ccache behind emcc rather than in front of emcc.bat, and
    MSVC's library names."""

    def test_cmake_lists_reach_cmake_unsplit(self):
        with mock.patch.object(subprocess, "run") as run:
            build.run_in_emsdk("emcmake",
                               ["cmake", "-DLLVM_ENABLE_PROJECTS=clang;lld",
                                "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG"],
                               Path("emsdk"), Path("."))
        self.assertEqual(run.call_args.args[0][-2:],
                         ["-DLLVM_ENABLE_PROJECTS=clang;lld",
                          "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG"])

    def test_ccache_moves_behind_emcc_on_windows_only(self):
        launchers = {"CMAKE_C_COMPILER_LAUNCHER": "ccache",
                     "CMAKE_CXX_COMPILER_LAUNCHER": "ccache"}
        with mock.patch.dict(os.environ, launchers):
            for windows in (False, True):
                with mock.patch.object(build, "WINDOWS", windows):
                    env = build.emsdk_env(Path("emsdk"))
                for k in launchers:
                    self.assertEqual(k in env, not windows, (k, windows))
                self.assertEqual(env.get("EM_COMPILER_WRAPPER"),
                                 "ccache" if windows else None)

    def test_static_libraries_are_found_under_both_naming_schemes(self):
        # MSVC builds the Windows native stage: LLVMSupport.lib, not
        # libLLVMSupport.a. Missing it shipped a native tree without libs.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "lib").mkdir()
            for name in ("libLLVMSupport.a", "LLVMTableGen.lib", "libclang.lib",
                         "lldWasm.lib", "libz.a", "LLVMSupport.pdb"):
                (Path(d) / "lib" / name).touch()
            self.assertEqual(sorted(build._walk_built_libs(Path(d))),
                             ["LLVMSupport", "LLVMTableGen", "libclang",
                              "lldWasm"])


class VersionTests(unittest.TestCase):
    def test_emsdk_is_named_by_the_version(self):
        self.assertEqual(build.parse_version("22"), ("22", build.DEFAULT_EMSDK))
        self.assertEqual(build.parse_version("22-emsdk6.0.8"), ("22", "6.0.8"))
        for bad in ("22.1", "22-emsdk", "22-emsdk6", "22-emsdklatest"):
            with self.assertRaises(ValueError, msg=bad):
                build.parse_version(bad)


if __name__ == "__main__":
    unittest.main()
