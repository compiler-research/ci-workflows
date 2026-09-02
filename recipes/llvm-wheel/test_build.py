"""Unit tests for the llvm-wheel recipe's version, container-reexec,
and artifact-contract logic.

Loaded by path rather than imported: recipe directories are not
packages, and every recipe's build script is called build.py, so a
plain import would collide across recipes in sys.modules.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "llvm_wheel_build", Path(__file__).resolve().parent / "build.py")
build = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build)

# The minimal runner-side env _reexec_in_container requires.
_REEXEC_ENV = {
    "WORK_DIR": "/tmp/w",
    "OUT_DIR": "/tmp/o",
    "RECIPE_VERSION": "21.1.8",
    "NCPUS": "4",
}


class SourceRefTests(unittest.TestCase):
    """A wheel toolchain must name exactly what it holds: only
    immutable release tags are accepted."""

    def test_exact_tag_is_accepted(self):
        self.assertEqual(build._source_ref("21.1.8"), "llvmorg-21.1.8")
        self.assertEqual(build._source_ref("23.1.0-rc2"),
                         "llvmorg-23.1.0-rc2")

    def test_partial_or_branch_versions_are_refused(self):
        for bad in ("21", "21.1", "21.1.8.1", "21.1.8-rc",
                    "release/21.x"):
            with self.assertRaises(ValueError):
                build._source_ref(bad)


class LinkJobsTests(unittest.TestCase):
    """Parallel LLVM links are the OOM vector on 16 GB runners."""

    def test_caps_at_four(self):
        with mock.patch.dict(os.environ, {"NCPUS": "16"}):
            self.assertEqual(build._link_jobs(), "4")

    def test_small_runners_keep_their_count(self):
        with mock.patch.dict(os.environ, {"NCPUS": "2"}):
            self.assertEqual(build._link_jobs(), "2")


class ManylinuxImageTests(unittest.TestCase):
    """The cell's arch picks the container the linux build runs in."""

    def test_default_is_x86_64(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build._manylinux_image(),
                             "quay.io/pypa/manylinux_2_28_x86_64")

    def test_aarch64_selects_the_arm_image(self):
        with mock.patch.dict(os.environ, {"RECIPE_ARCH": "aarch64"}):
            self.assertEqual(build._manylinux_image(),
                             "quay.io/pypa/manylinux_2_28_aarch64")


class ReexecTests(unittest.TestCase):
    """The docker command must carry the runner-side contract into the
    container: required env always, ccache and GITHUB_ENV only when
    the runner provides them."""

    def _docker_cmd(self, extra_env):
        env = dict(_REEXEC_ENV, **extra_env)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(build.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            rc = build._reexec_in_container()
        self.assertEqual(rc, 0)
        return run.call_args[0][0]

    def test_required_mounts_and_env(self):
        cmd = self._docker_cmd({})
        self.assertIn("/tmp/w:/work", cmd)
        self.assertIn("/tmp/o:/out", cmd)
        self.assertIn("RECIPE_VERSION=21.1.8", cmd)
        self.assertIn("NCPUS=4", cmd)
        self.assertNotIn("CCACHE_DIR=/ccache", cmd)
        self.assertNotIn("GITHUB_ENV=/github_env", cmd)

    def test_ccache_mount_only_when_configured(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = self._docker_cmd({
                "CCACHE_DIR": f"{d}/cc",
                "CMAKE_C_COMPILER_LAUNCHER": "ccache",
            })
        self.assertIn(f"{d}/cc:/ccache", cmd)
        self.assertIn("CCACHE_DIR=/ccache", cmd)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER=ccache", cmd)

    def test_github_env_is_remapped(self):
        cmd = self._docker_cmd({"GITHUB_ENV": "/tmp/ge"})
        self.assertIn("/tmp/ge:/github_env", cmd)
        self.assertIn("GITHUB_ENV=/github_env", cmd)

    def test_aarch64_cell_runs_the_arm_image(self):
        cmd = self._docker_cmd({"RECIPE_ARCH": "aarch64"})
        self.assertIn("quay.io/pypa/manylinux_2_28_aarch64", cmd)
        self.assertIn("RECIPE_ARCH=aarch64", cmd)


class StaticOnlyTests(unittest.TestCase):
    """The wheel contract bans LLVM/clang dylibs in the artifact."""

    def test_clean_prefix_passes(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib"
            lib.mkdir()
            (lib / "libLLVMCore.a").touch()
            build._static_only_or_die(Path(d))

    def test_dylib_fails(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib"
            lib.mkdir()
            (lib / "libLLVM-21.so").touch()
            with self.assertRaises(RuntimeError):
                build._static_only_or_die(Path(d))


if __name__ == "__main__":
    unittest.main()
