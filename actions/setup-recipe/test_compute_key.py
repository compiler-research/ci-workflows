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

import bootstrap_block
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

    def test_lib_content_invalidates(self):
        # actions/lib/ Python contents must move the key: changes
        # there reshape the published artifact (component lists,
        # smoke checks), so old cells must stop shadowing new code.
        lib = Path(self.dir) / "actions_lib"
        lib.mkdir()
        (lib / "llvm_build.py").write_text("orig\n")
        with_lib = self._key(lib_root=str(lib))
        self.assertNotEqual(self.base, with_lib)
        (lib / "llvm_build.py").write_text("changed\n")
        edited = self._key(lib_root=str(lib))
        self.assertNotEqual(with_lib, edited)

    def test_lib_test_files_ignored(self):
        # test_*.py and __pycache__ must NOT contribute -- editing
        # tests or running tests (which populates __pycache__) would
        # otherwise invalidate every cached artifact for free.
        lib = Path(self.dir) / "actions_lib"
        lib.mkdir()
        (lib / "llvm_build.py").write_text("hi\n")
        baseline = self._key(lib_root=str(lib))
        (lib / "test_llvm_build.py").write_text("import x\n")
        (lib / "__pycache__").mkdir()
        (lib / "__pycache__" / "x.pyc").write_bytes(b"\x00bytecode")
        self.assertEqual(baseline, self._key(lib_root=str(lib)))


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


class BootstrapTests(unittest.TestCase):
    """A recipe built on another is keyed on that one too.

    Without it a dependent's key stands still while the bootstrap it is
    linked against changes: the published artifact keeps answering "already
    built" for content it was not built from, and a pull request editing only
    the bootstrap never rebuilds anything on top of it.
    """

    def _tree(self, d: Path, *, bootstrap_version: str = "{version}",
              lr_build: str = "#!/usr/bin/env bash\nexit 0\n") -> Path:
        recipes = d / "recipes"
        _make_recipe(recipes, "llvm-release", build_sh=lr_build)
        _make_recipe(
            recipes, "dep",
            yaml=("recipe: dep\n"
                  "bootstrap:\n"
                  "  recipe: llvm-release\n"
                  f"  version: '{bootstrap_version}'\n"))
        (d / "lib").mkdir()
        return recipes

    def _key(self, recipes: Path, d: Path, recipe: str = "dep") -> str:
        return compute_key.compute_key(
            recipe, "22", "ubuntu-24.04", "x86_64",
            recipe_root=str(recipes), lib_root=str(d / "lib"))

    def test_bootstrap_edit_moves_the_dependent_key(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            recipes = self._tree(d)
            before = self._key(recipes, d)
            (recipes / "llvm-release" / "build.sh").write_text(
                "#!/usr/bin/env bash\nexit 1\n")
            self.assertNotEqual(before, self._key(recipes, d))

    def test_recipe_without_bootstrap_is_unaffected(self):
        """The hash of a recipe that declares none must not shift."""
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            recipes = self._tree(d)
            before = self._key(recipes, d, "llvm-release")
            (recipes / "dep" / "recipe.yaml").write_text("recipe: dep\n")
            self.assertEqual(before, self._key(recipes, d, "llvm-release"))

    def test_placeholder_resolves_against_the_dependent_version(self):
        """The cell folded in is the one the build will actually fetch.

        Asserted on the resolution rather than on two keys: recipe.yaml is
        itself hashed, so two recipes declaring the bootstrap differently
        have different keys whatever the placeholder does.
        """
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            recipes = self._tree(d)
            self.assertEqual(
                bootstrap_block.bootstrap_cell(recipes / "dep", "23"),
                ("llvm-release", "23"))
            self.assertEqual(
                bootstrap_block.bootstrap_cell(recipes / "llvm-release", "23"),
                None)

    def test_cycle_is_reported_not_recursed(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            recipes = d / "recipes"
            _make_recipe(recipes, "a",
                         yaml=("recipe: a\nbootstrap:\n"
                               "  recipe: b\n  version: '22'\n"))
            _make_recipe(recipes, "b",
                         yaml=("recipe: b\nbootstrap:\n"
                               "  recipe: a\n  version: '22'\n"))
            (d / "lib").mkdir()
            with self.assertRaises(ValueError) as cm:
                compute_key.compute_key(
                    "a", "22", "ubuntu-24.04", "x86_64",
                    recipe_root=str(recipes), lib_root=str(d / "lib"))
            self.assertIn("bootstrap cycle", str(cm.exception))

    def test_half_written_block_is_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            recipes = d / "recipes"
            _make_recipe(recipes, "dep",
                         yaml="recipe: dep\nbootstrap:\n  recipe: llvm-release\n")
            (d / "lib").mkdir()
            with self.assertRaises(ValueError):
                compute_key.compute_key(
                    "dep", "22", "ubuntu-24.04", "x86_64",
                    recipe_root=str(recipes), lib_root=str(d / "lib"))

