"""Unit tests for compute_key.

Covers determinism, the bash-predecessor byte-for-byte parity (so
existing keys for recipes-with-build.sh don't shift), perturbation
sensitivity (every input we claim invalidates the key actually
moves it), and the build.sh / build.py fallback ordering.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import compute_key


def _make_recipe(root: Path, name: str, *,
                 yaml: str = "recipe: x\n",
                 build_sh: str = "#!/usr/bin/env bash\nexit 0\n",
                 build_py: str = "",
                 patches: dict[str, str] = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "recipe.yaml").write_text(yaml)
    if build_sh:
        (d / "build.sh").write_text(build_sh)
    if build_py:
        (d / "build.py").write_text(build_py)
    if patches:
        (d / "patches").mkdir()
        for rel, content in patches.items():
            target = d / "patches" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    return d


class DeterminismTests(unittest.TestCase):
    def test_same_inputs_same_key(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "r")
            a = compute_key.compute_key("r", "22", "ubuntu-24.04", "x86_64", d)
            b = compute_key.compute_key("r", "22", "ubuntu-24.04", "x86_64", d)
            self.assertEqual(a, b)

    def test_relative_vs_absolute_root_match(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "r")
            cwd = os.getcwd()
            try:
                os.chdir(d)
                a = compute_key.compute_key("r", "22", "ubuntu-24.04", "x86_64", ".")
                b = compute_key.compute_key("r", "22", "ubuntu-24.04", "x86_64", d)
                self.assertEqual(a, b)
            finally:
                os.chdir(cwd)


class KeyShapeTests(unittest.TestCase):
    def test_format(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "myrecipe")
            key = compute_key.compute_key("myrecipe", "v1", "linux", "arm", d)
            self.assertTrue(key.startswith("myrecipe-v1-linux-arm-"))
            short = key.rsplit("-", 1)[1]
            self.assertEqual(len(short), 16)
            int(short, 16)  # must be valid hex


class PerturbationTests(unittest.TestCase):
    """Each input we claim invalidates the key must actually move it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        _make_recipe(Path(self.dir), "r",
                     yaml="recipe: r\nsource:\n  repo: x\n",
                     patches={"a.patch": "diff content"})
        self.base = compute_key.compute_key(
            "r", "22", "ubuntu-24.04", "x86_64", self.dir
        )

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _key(self, **overrides):
        kwargs = dict(recipe="r", version="22",
                      os_="ubuntu-24.04", arch="x86_64",
                      recipe_root=self.dir)
        kwargs.update(overrides)
        return compute_key.compute_key(**kwargs)

    def test_version(self):
        self.assertNotEqual(self.base, self._key(version="99"))

    def test_os(self):
        self.assertNotEqual(self.base, self._key(os_="macos-26"))

    def test_arch(self):
        self.assertNotEqual(self.base, self._key(arch="arm64"))

    def test_recipe_yaml_edit(self):
        (Path(self.dir) / "r" / "recipe.yaml").write_text("changed\n")
        self.assertNotEqual(self.base, self._key())

    def test_build_sh_edit(self):
        (Path(self.dir) / "r" / "build.sh").write_text("changed\n")
        self.assertNotEqual(self.base, self._key())

    def test_patch_content_edit(self):
        (Path(self.dir) / "r" / "patches" / "a.patch").write_text("new\n")
        self.assertNotEqual(self.base, self._key())

    def test_patch_added(self):
        (Path(self.dir) / "r" / "patches" / "b.patch").write_text("new\n")
        self.assertNotEqual(self.base, self._key())

    def test_patch_renamed(self):
        (Path(self.dir) / "r" / "patches" / "a.patch").rename(
            Path(self.dir) / "r" / "patches" / "renamed.patch"
        )
        self.assertNotEqual(self.base, self._key())


class BuildScriptFallbackTests(unittest.TestCase):
    def test_build_sh_preferred_when_both(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "r",
                         build_sh="A\n", build_py="B\n")
            key_with_both = compute_key.compute_key(
                "r", "22", "ubuntu-24.04", "x86_64", d
            )
            # Removing build.py must not move the key (because we ignore it
            # when build.sh is present).
            (Path(d) / "r" / "build.py").unlink()
            key_no_py = compute_key.compute_key(
                "r", "22", "ubuntu-24.04", "x86_64", d
            )
            self.assertEqual(key_with_both, key_no_py)

    def test_build_py_used_when_no_sh(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "r",
                         build_sh="", build_py="print('hi')\n")
            self.assertFalse((Path(d) / "r" / "build.sh").exists())
            key = compute_key.compute_key(
                "r", "22", "ubuntu-24.04", "x86_64", d
            )
            self.assertTrue(key.startswith("r-22-ubuntu-24.04-x86_64-"))

    def test_no_build_script_raises(self):
        with tempfile.TemporaryDirectory() as d:
            d_recipe = Path(d) / "r"
            d_recipe.mkdir()
            (d_recipe / "recipe.yaml").write_text("recipe: r\n")
            with self.assertRaises(FileNotFoundError):
                compute_key.compute_key(
                    "r", "22", "ubuntu-24.04", "x86_64", d
                )


def _have_bash_predecessor() -> bool:
    """Check if compute-key.sh and sha256sum + awk are available."""
    if not (Path(__file__).parent / "compute-key.sh").is_file():
        return False
    for tool in ("sha256sum", "awk"):
        if not shutil.which(tool):
            return False
    return True


@unittest.skipUnless(_have_bash_predecessor(), "bash predecessor unavailable")
class BashParityTests(unittest.TestCase):
    """Python version must produce identical keys to compute-key.sh
    on recipes that have build.sh — otherwise migration would orphan
    every existing cache asset."""

    def _run_bash(self, recipe_root, recipe, version, os_, arch):
        result = subprocess.run(
            ["bash", str(Path(__file__).parent / "compute-key.sh"),
             recipe, version, os_, arch, recipe_root],
            capture_output=True, text=True, check=True,
        )
        # stdout is "key=<value>\n"
        return result.stdout.strip().split("=", 1)[1]

    def test_minimal_recipe(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(Path(d), "r",
                         yaml="recipe: r\n", build_sh="echo hi\n")
            py_key = compute_key.compute_key(
                "r", "22", "ubuntu-24.04", "x86_64", d
            )
            sh_key = self._run_bash(d, "r", "22", "ubuntu-24.04", "x86_64")
            self.assertEqual(py_key, sh_key)

    def test_recipe_with_patches(self):
        with tempfile.TemporaryDirectory() as d:
            _make_recipe(
                Path(d), "r",
                patches={"a.patch": "x", "nested/b.patch": "y"},
            )
            py_key = compute_key.compute_key(
                "r", "22", "ubuntu-24.04", "x86_64", d
            )
            sh_key = self._run_bash(d, "r", "22", "ubuntu-24.04", "x86_64")
            self.assertEqual(py_key, sh_key)


if __name__ == "__main__":
    unittest.main()
