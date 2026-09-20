"""Unit tests for the llvm-wasm recipe's version-conditional pieces.

Loaded by path rather than imported: recipe directories are not
packages ('llvm-wasm' is not an identifier), and every recipe's build
script is called build.py, so a plain import would collide across
recipes in sys.modules.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "llvm_wasm_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)


class MajorTests(unittest.TestCase):
    """Everything version-conditional keys off the major, so a cell
    pinned to a tag has to resolve to the same major as the bare one."""

    def test_bare_major(self):
        self.assertEqual(build._major("22"), 22)
        self.assertEqual(build._major("23"), 23)

    def test_tag_shaped_version(self):
        self.assertEqual(build._major("23.1.0-rc2"), 23)
        self.assertEqual(build._major("22.1.8"), 22)

    def test_unparseable_version_aborts(self):
        # Silently proceeding would publish a cell with no patches and
        # no -mtail-call under a name that claims a major.
        with self.assertRaises(SystemExit):
            build._major("cling-llvm22")


class LangFlagsTests(unittest.TestCase):
    """-mtail-call is gated on LLVM >= 23: wasm-ld rejects a link whose
    objects disagree about the enabled feature set, so applying it to 22
    would break consumers that compile without it."""

    def test_22_keeps_only_the_wait4_define(self):
        self.assertEqual(build._lang_flags(22),
                         ["-DCMAKE_CXX_FLAGS=-Dwait4=__syscall_wait4"])

    def test_23_adds_tail_call_to_both_languages(self):
        flags = build._lang_flags(23)
        self.assertIn("-DCMAKE_C_FLAGS=-mtail-call", flags)
        self.assertIn("-DCMAKE_CXX_FLAGS=-mtail-call -Dwait4=__syscall_wait4",
                      flags)

    def test_wait4_survives_the_gate(self):
        # The define is what makes emscripten's libc build at all; a
        # refactor that drops it while adding -mtail-call would fail
        # deep in the compile rather than here.
        for major in (22, 23, 24):
            self.assertTrue(
                any("wait4=__syscall_wait4" in f
                    for f in build._lang_flags(major)),
                msg=f"wait4 define missing for major {major}")


class PicFlagTests(unittest.TestCase):
    def test_pic_is_off_for_the_wasm_stage(self):
        # Not a style choice: LLVM_ENABLE_PIC is what gates tools/lto,
        # whose SHARED libLTO a wasm target cannot produce -- a hard
        # configure error from LLVM 23 on.
        self.assertIn("-DLLVM_ENABLE_PIC=OFF", build.COMMON_FLAGS)


class BuildScopeFlagsTests(unittest.TestCase):
    """The cell ships libraries, headers and cmake exports -- no
    binaries. Each switch below keeps a family of executables out of the
    default build; the set matches emscripten-forge's llvm recipe."""

    def test_no_binaries_are_built_by_default(self):
        for flag in ("-DLLVM_BUILD_TOOLS=OFF", "-DLLVM_BUILD_UTILS=OFF",
                     "-DCLANG_BUILD_TOOLS=OFF", "-DLLD_BUILD_TOOLS=OFF"):
            self.assertIn(flag, build.COMMON_FLAGS)

    def test_lld_libraries_stay_in_the_distribution(self):
        # LLD_BUILD_TOOLS=OFF drops lld's executables, not its libraries:
        # clangInterpreter links lldCommon/lldWasm, and dropping those
        # would break the ClangTargets export rather than fail visibly.
        components = build._wasm_dist_components(Path("/nonexistent"))
        self.assertIn("lld-cmake-exports", components)
        self.assertIn("lld-headers", components)


class ApplyPatchesTests(unittest.TestCase):
    def _patch_set(self, major, names):
        """Run apply_patches against a synthetic patches/ dir; return the
        patch filenames git apply was called with, in call order."""
        with tempfile.TemporaryDirectory() as d:
            patches = Path(d) / "patches"
            patches.mkdir()
            for n in names:
                (patches / n).write_text("")
            with mock.patch.object(build, "SCRIPT_DIR", Path(d)), \
                    mock.patch.object(build, "subprocess") as sp:
                build.apply_patches(Path(d) / "repo", major)
            return [Path(c.args[0][-1]).name for c in sp.run.call_args_list]

    def test_only_the_matching_major_is_applied(self):
        applied = self._patch_set(23, [
            "emscripten-clang22-1-a.patch",
            "emscripten-clang22-2-b.patch",
            "emscripten-clang23-1-a.patch",
        ])
        self.assertEqual(applied, ["emscripten-clang23-1-a.patch"])

    def test_lexical_order_within_a_major(self):
        applied = self._patch_set(22, [
            "emscripten-clang22-2-b.patch",
            "emscripten-clang22-1-a.patch",
        ])
        self.assertEqual(applied, ["emscripten-clang22-1-a.patch",
                                   "emscripten-clang22-2-b.patch"])

    def test_major_without_patches_is_a_no_op(self):
        self.assertEqual(self._patch_set(19, ["emscripten-clang22-1-a.patch"]),
                         [])

    def test_shipped_patch_sets(self):
        """Pins what the repo actually carries per major.

        23 gets one patch, not a copy of 22's second: the
        WebAssemblyTargetMachine reordering landed upstream in
        release/23.x, and `git apply` of an already-applied patch fails
        the build under check=True.
        """
        patches = Path(__file__).resolve().parent / "patches"
        by_major = {}
        for p in sorted(patches.glob("emscripten-clang*.patch")):
            major = p.name.split("-")[1].removeprefix("clang")
            by_major.setdefault(major, []).append(p.name)
        self.assertEqual(sorted(by_major), ["22", "23"])
        self.assertEqual(len(by_major["22"]), 2)
        self.assertEqual(by_major["23"],
                         ["emscripten-clang23-1-enable_exception_handling.patch"])


if __name__ == "__main__":
    unittest.main()
