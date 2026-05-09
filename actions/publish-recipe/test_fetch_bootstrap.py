"""Unit tests for fetch_bootstrap."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fetch_bootstrap


def _write_recipe_yaml(d: Path, *, with_bootstrap: bool = False,
                       partial: bool = False) -> Path:
    """Write a minimal recipe.yaml. with_bootstrap adds a complete
    block; partial omits one of the two required subkeys.
    """
    body = (
        "recipe: testrec\n"
        "description: test fixture\n"
        "source:\n"
        "  repo: https://example.com/llvm-project\n"
        "  branch_template: release/{version}.x\n"
    )
    if with_bootstrap:
        body += "bootstrap:\n  recipe: llvm-release\n"
        if not partial:
            body += "  version: '22'\n"
    elif partial:
        # partial without with_bootstrap: only `version`, no `recipe`.
        body += "bootstrap:\n  version: '22'\n"
    yaml_path = d / "recipe.yaml"
    yaml_path.write_text(body)
    return yaml_path


class GrepYamlBlockFieldTests(unittest.TestCase):
    """Direct test for the YAML block-field grepper.

    Most positive / negative paths are covered indirectly through
    MainTests (which proves the grepper's outputs reach compute_key
    and trigger the correct main() branch). The one path MainTests
    cannot reach: cross-block scope bleed -- main() only ever queries
    `bootstrap.recipe` and `bootstrap.version`, which never collide
    with `source.*` in production. A regression where the grepper
    returned a `source.repo` value for a `bootstrap.repo` query would
    pass every integration test silently.
    """

    def test_block_scope_does_not_leak(self):
        with tempfile.TemporaryDirectory() as d:
            y = _write_recipe_yaml(Path(d), with_bootstrap=True)
            self.assertIsNone(
                fetch_bootstrap._grep_yaml_block_field(
                    y, "bootstrap", "repo"),
            )
            # Sanity: the same key IS readable inside its own block.
            self.assertEqual(
                fetch_bootstrap._grep_yaml_block_field(
                    y, "source", "repo"),
                "https://example.com/llvm-project",
            )


class MainTests(unittest.TestCase):
    """End-to-end tests of main() with cache_io and compute_key mocked.

    cache mocking matters because the real cache_io would try to hit
    GitHub Releases for an existing cell on every test run, making the
    tests both slow and network-dependent.
    """

    def _run_main(self, argv):
        """Invoke main() with sys.argv overridden, capturing stdout/err."""
        old_argv, old_stdout, old_stderr = sys.argv, sys.stdout, sys.stderr
        sys.argv = argv
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            rc = fetch_bootstrap.main()
            return rc, sys.stdout.getvalue(), sys.stderr.getvalue()
        finally:
            sys.argv, sys.stdout, sys.stderr = (
                old_argv, old_stdout, old_stderr,
            )

    def test_no_bootstrap_block_silent_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir)  # no bootstrap
            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out, "")
            self.assertEqual(err, "")

    def test_partial_bootstrap_block_errors(self):
        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, partial=True)  # version only
            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("incomplete bootstrap block", err)

    def test_missing_recipe_yaml_errors(self):
        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()  # no recipe.yaml inside
            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("no recipe.yaml", err)

    def test_bad_argv_returns_2(self):
        rc, _, err = self._run_main(["fetch_bootstrap.py", "only-one-arg"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    @mock.patch("fetch_bootstrap.subprocess.run")
    @mock.patch("fetch_bootstrap.cache_io.cache_download")
    @mock.patch("fetch_bootstrap.cache_io.cache_probe")
    @mock.patch("fetch_bootstrap.cache_io.resolve_cache_base")
    def test_cache_miss_errors_with_actionable_message(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        mock_resolve.return_value = "file:///fake/cache"
        mock_probe.return_value = False
        mock_run.return_value = mock.Mock(stdout="key=fake-key\n")
        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, with_bootstrap=True)
            rc, _, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("not in cache", err)
            self.assertIn("Publish that cell first", err)
            mock_dl.assert_not_called()

    @mock.patch("fetch_bootstrap.subprocess.run")
    @mock.patch("fetch_bootstrap.cache_io.cache_download")
    @mock.patch("fetch_bootstrap.cache_io.cache_probe")
    @mock.patch("fetch_bootstrap.cache_io.resolve_cache_base")
    def test_cache_hit_prints_bin_dir(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        mock_resolve.return_value = "file:///fake/cache"
        mock_probe.return_value = True
        mock_run.return_value = mock.Mock(stdout="key=fake-key\n")

        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, with_bootstrap=True)
            download_dir = Path(d) / "dl"

            # Simulate cache_download materialising the expected layout.
            def _materialise(base, key, out_dir):
                bin_dir = Path(out_dir) / "llvm-project" / "bin"
                bin_dir.mkdir(parents=True)
                (bin_dir / "clang").write_text("#!/bin/false\n")
                (bin_dir / "clang").chmod(0o755)
            mock_dl.side_effect = _materialise

            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64", str(download_dir)],
            )
            self.assertEqual(rc, 0, msg=err)
            # main() resolves the download_dir before printing, so the
            # expected path needs the same resolution to compare on
            # macOS where /var symlinks to /private/var.
            expected_bin = (
                download_dir.resolve() / "llvm-project" / "bin"
            )
            self.assertEqual(out.strip(), str(expected_bin))
            # Verify compute_key.py was called with the bootstrap
            # block's recipe/version (not the consuming recipe's name)
            # — guards against argv reordering regressions.
            args = mock_run.call_args.args[0]
            self.assertIn("llvm-release", args)
            self.assertIn("22", args)
            self.assertIn("ubuntu-24.04", args)
            self.assertIn("x86_64", args)

    @mock.patch("fetch_bootstrap.subprocess.run")
    @mock.patch("fetch_bootstrap.cache_io.cache_download")
    @mock.patch("fetch_bootstrap.cache_io.cache_probe")
    @mock.patch("fetch_bootstrap.cache_io.resolve_cache_base")
    def test_default_download_dir_is_recipe_sibling(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        """4-arg form: download lands at <recipe_dir>/_bootstrap.

        This is the path publish-recipe.yml's pre-build step uses by
        default; covered explicitly so a future refactor of that
        default does not silently break the workflow.
        """
        mock_resolve.return_value = "file:///fake/cache"
        mock_probe.return_value = True
        mock_run.return_value = mock.Mock(stdout="key=fake-key\n")

        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, with_bootstrap=True)

            def _materialise(base, key, out_dir):
                bin_dir = Path(out_dir) / "llvm-project" / "bin"
                bin_dir.mkdir(parents=True)
                (bin_dir / "clang").write_text("#!/bin/false\n")
                (bin_dir / "clang").chmod(0o755)
            mock_dl.side_effect = _materialise

            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 0, msg=err)
            expected_bin = (
                (recipe_dir / "_bootstrap").resolve()
                / "llvm-project" / "bin"
            )
            self.assertEqual(out.strip(), str(expected_bin))

    @mock.patch("fetch_bootstrap.subprocess.run")
    @mock.patch("fetch_bootstrap.cache_io.cache_download")
    @mock.patch("fetch_bootstrap.cache_io.cache_probe")
    @mock.patch("fetch_bootstrap.cache_io.resolve_cache_base")
    def test_extracted_cell_missing_clang_errors(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        """Cell layout drift: download succeeds but bin/clang absent.

        Pins the post-extract sanity check; the bootstrap clang's
        path is the contract publish-recipe's BOOTSTRAP_CLANG_BIN
        export depends on.
        """
        mock_resolve.return_value = "file:///fake/cache"
        mock_probe.return_value = True
        mock_run.return_value = mock.Mock(stdout="key=fake-key\n")
        # cache_download "succeeds" but never writes bin/clang.
        mock_dl.return_value = None

        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, with_bootstrap=True)
            rc, _, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir),
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("cell layout changed", err)


if __name__ == "__main__":
    unittest.main()
