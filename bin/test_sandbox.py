"""Pin every docker command line bin/sandbox.py produces.

These are deliberately exact: what a container may see and do is the
point of the module, so a change to it should surface here as a diff a
reviewer reads, not slip through as a side effect.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402


def _load_repro():
    path = Path(__file__).resolve().parent / "repro"
    loader = importlib.machinery.SourceFileLoader("repro", str(path))
    spec = importlib.util.spec_from_loader("repro", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class ArgvTest(unittest.TestCase):
    def test_mount_modes(self):
        self.assertEqual(sandbox.Mount("/a", "/b").argv(), ["-v", "/a:/b"])
        self.assertEqual(sandbox.Mount("/a", "/b", readonly=True).argv(),
                         ["-v", "/a:/b:ro"])

    def test_exec_argv(self):
        self.assertEqual(sandbox.exec_argv("c", ["bash"]),
                         ["docker", "exec", "c", "bash"])
        self.assertEqual(
            sandbox.exec_argv("c", ["bash"], user="dev", workdir="/w",
                              tty=True),
            ["docker", "exec", "-it", "-u", "dev", "-w", "/w", "c", "bash"])

    def test_spec_order_puts_caller_options_last(self):
        spec = sandbox.DevshellSpec(
            name="n", image="img", platform="linux/amd64", workdir="/w",
            mounts=[sandbox.Mount("vol", "/w"),
                    sandbox.Mount("/src", "/ro", readonly=True)],
            env=[("A", "1"), ("B", "x y")], labels={"l": "v"},
            security=["--cap-drop=ALL"], caller_options="--gpus all")
        self.assertEqual(spec.run_argv(), [
            "docker", "run", "-d", "--platform", "linux/amd64", "--name", "n",
            "-v", "vol:/w", "-v", "/src:/ro:ro", "-w", "/w",
            "-e", "A=1", "-e", "B=x y", "--label", "l=v",
            "--cap-drop=ALL", "--gpus", "all", "img", "sleep", "infinity"])

    def test_oneshot(self):
        with mock.patch.object(sandbox.subprocess, "run") as run:
            sandbox.oneshot("img", ["true"], mounts=[sandbox.Mount("v", "/w")],
                            platform="linux/arm64")
        self.assertEqual(run.call_args[0][0], [
            "docker", "run", "--rm", "--platform", "linux/arm64",
            "-v", "v:/w", "img", "true"])


class NothingElseBuildsDockerCommandsTest(unittest.TestCase):
    def test_repro_has_no_docker_argv_of_its_own(self):
        # The audit surface is sandbox.py; a `["docker", ...]` list
        # anywhere else in bin/repro would be a second one.
        src = (Path(__file__).resolve().parent / "repro").read_text(
            encoding="utf-8")
        self.assertNotIn('"docker",', src)
        self.assertNotIn("'docker',", src)


class DevshellRunTest(unittest.TestCase):
    """The full `docker run` of a devshell, as bin/repro builds it."""

    def test_devshell_run_argv(self):
        repro = _load_repro()
        manifest = {"source": {"repo": "https://github.com/llvm/llvm-project"},
                    "build_env": {"ccache": {
                        "compiler_check": "string:clang 18", "hash_dir": "false",
                        "base_dir": "/home/runner/work/ci-workflows/ci-workflows",
                        "locale": {"LANG": "C.UTF-8"}}}}
        args = repro.parse_args(["--devshell"])
        with mock.patch.object(repro, "_devshell_container_exists",
                               return_value=False), \
                mock.patch.object(repro, "_devshell_host_uid_gid",
                                  return_value=(501, 20)), \
                mock.patch.object(sandbox.subprocess, "run") as run:
            repro._devshell_ensure_container(
                args, "devshell-x", "img", manifest["build_env"]["ccache"]
                ["base_dir"], manifest, volume_name=None,
                work_host_bind=Path("/hc/cells/x"), host_cache=Path("/hc"),
                patches_out=Path("/Users/me/src/clad"),
                docker_platform="linux/amd64")
        argv = run.call_args[0][0]
        ws = "/home/runner/work/ci-workflows/ci-workflows"
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        self.assertEqual(mounts, [
            f"{Path('/hc/cells/x')}:{ws}",
            f"{repro.REPO_ROOT}:/ci-workflows:ro",
            f"{Path('/Users/me/src/clad')}:/patches",
            f"{Path('/hc')}:/cache",
        ])
        self.assertEqual(argv[:7], ["docker", "run", "-d", "--platform",
                                    "linux/amd64", "--name", "devshell-x"])
        self.assertEqual(argv[-3:], ["img", "sleep", "infinity"])


if __name__ == "__main__":
    unittest.main()
