"""Unit tests for build_manifest."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_manifest


def _make_recipe(root: Path, name: str, *,
                 repo: str = "https://github.com/llvm/llvm-project",
                 branch_template: str = "release/{version}.x",
                 build_sh: str = "echo hi\n",
                 build_py: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    yaml = (
        f"recipe: {name}\n"
        f"description: test fixture\n"
        f"source:\n"
        f"  repo: {repo}\n"
        f"  branch_template: {branch_template}\n"
    )
    (d / "recipe.yaml").write_text(yaml)
    if build_sh:
        (d / "build.sh").write_text(build_sh)
    if build_py:
        (d / "build.py").write_text(build_py)
    return d


class GrepYamlValueTests(unittest.TestCase):
    def test_first_match_wins(self):
        with tempfile.TemporaryDirectory() as d:
            y = Path(d) / "y.yaml"
            y.write_text(
                "source:\n"
                "  repo: https://example.com/a\n"
                "extra:\n"
                "  repo: https://example.com/b\n"
            )
            self.assertEqual(
                build_manifest._grep_yaml_value(y, "repo"),
                "https://example.com/a",
            )

    def test_strips_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            y = Path(d) / "y.yaml"
            y.write_text('  branch_template: "release/{version}.x"\n')
            self.assertEqual(
                build_manifest._grep_yaml_value(y, "branch_template"),
                "release/{version}.x",
            )

    def test_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            y = Path(d) / "y.yaml"
            y.write_text("recipe: foo\n")
            self.assertIsNone(
                build_manifest._grep_yaml_value(y, "repo")
            )


class BuildManifestTests(unittest.TestCase):
    def test_basic_shape(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {
                 "SRC_COMMIT": "abc1234",
                 "GITHUB_SHA": "deadbeef",
                 "ImageOS": "ubuntu24",
                 "ImageVersion": "20240101.1",
             }, clear=True):
            _make_recipe(Path(d), "myrecipe")
            m = build_manifest.build_manifest(
                "myrecipe", "22", "ubuntu-24.04", "x86_64",
                "myrecipe-22-ubuntu-24.04-x86_64-deadbeef00000000",
                recipe_root=d,
            )
            # Round-trip JSON to verify it's serializable.
            self.assertEqual(json.loads(json.dumps(m)), m)

            self.assertEqual(m["recipe"],  "myrecipe")
            self.assertEqual(m["version"], "22")
            self.assertEqual(m["platform"]["os"],   "ubuntu-24.04")
            self.assertEqual(m["platform"]["arch"], "x86_64")
            self.assertEqual(m["platform"]["runner_image"], "ubuntu24")
            self.assertEqual(m["platform"]["runner_image_version"], "20240101.1")
            self.assertEqual(m["source"]["repo"],
                             "https://github.com/llvm/llvm-project")
            self.assertEqual(m["source"]["branch"], "release/22.x")
            self.assertEqual(m["source"]["commit"], "abc1234")
            self.assertEqual(m["ci_workflows_sha"], "deadbeef")
            self.assertEqual(m["build_script"], "build.sh")
            # built_at is ISO-8601 UTC.
            self.assertRegex(m["built_at"],
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_missing_env_defaults_unknown(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {}, clear=True):
            _make_recipe(Path(d), "r")
            m = build_manifest.build_manifest(
                "r", "1", "ubuntu-24.04", "x86_64", "r-1-ubuntu-24.04-x86_64-x",
                recipe_root=d,
            )
            self.assertEqual(m["source"]["commit"], "unknown")
            self.assertEqual(m["ci_workflows_sha"], "unknown")
            self.assertEqual(m["platform"]["runner_image"], "unknown")

    def test_branch_template_substitution(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {}, clear=True):
            _make_recipe(Path(d), "r",
                         branch_template="{version}-some-tag")
            m = build_manifest.build_manifest(
                "r", "ROOT-llvm20", "ubuntu-24.04", "x86_64", "k",
                recipe_root=d,
            )
            self.assertEqual(m["source"]["branch"], "ROOT-llvm20-some-tag")

    def test_build_py_recipe(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {}, clear=True):
            _make_recipe(Path(d), "r",
                         build_sh="", build_py="print('x')\n")
            m = build_manifest.build_manifest(
                "r", "1", "ubuntu-24.04", "x86_64", "k",
                recipe_root=d,
            )
            self.assertEqual(m["build_script"], "build.py")
            # build_sh_sha256 is the legacy alias; should still be set.
            self.assertEqual(
                m["build_script_sha256"], m["build_sh_sha256"]
            )
            # And it must be a real hash (64 hex chars), not "unknown".
            self.assertRegex(m["build_script_sha256"], r"^[0-9a-f]{64}$")

    def test_missing_recipe_yaml_unknown(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {}, clear=True):
            d_recipe = Path(d) / "r"
            d_recipe.mkdir()
            (d_recipe / "build.sh").write_text("echo\n")
            m = build_manifest.build_manifest(
                "r", "1", "ubuntu-24.04", "x86_64", "k",
                recipe_root=d,
            )
            self.assertEqual(m["recipe_yaml_sha256"], "unknown")
            self.assertEqual(m["source"]["repo"], "unknown")
            self.assertEqual(m["source"]["branch"], "unknown")


if __name__ == "__main__":
    unittest.main()
