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
                       partial: bool = False,
                       bootstrap_version: str = "22") -> Path:
    """Write a minimal recipe.yaml. with_bootstrap adds a complete
    block; partial omits one of the two required subkeys.
    bootstrap_version is written verbatim, so pass '{version}' to
    exercise the placeholder path.
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
            body += f"  version: '{bootstrap_version}'\n"
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


class ResolveBootstrapVersionTests(unittest.TestCase):
    """The `{version}` placeholder in a bootstrap block.

    A recipe published at more than one major has to bootstrap from a
    different provider cell per major (llvm-msan 23 cannot be built by
    the clang-22 bootstrap that llvm-msan 22 wants). A literal must
    still pass through untouched so recipes pinning one bootstrap
    regardless of their own version keep resolving.
    """

    def test_literal_passes_through(self):
        self.assertEqual(
            fetch_bootstrap._resolve_bootstrap_version("22", "23"), "22")

    def test_placeholder_substitutes_recipe_version(self):
        self.assertEqual(
            fetch_bootstrap._resolve_bootstrap_version("{version}", "23"),
            "23")

    def test_placeholder_keeps_non_major_version_strings(self):
        # Versions aren't always bare majors (llvm-release publishes
        # '23.1.0-rc2'), so the substitution must be textual, not numeric.
        self.assertEqual(
            fetch_bootstrap._resolve_bootstrap_version(
                "{version}", "23.1.0-rc2"),
            "23.1.0-rc2")


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
                ["fetch_bootstrap.py", str(recipe_dir), "22",
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
                ["fetch_bootstrap.py", str(recipe_dir), "22",
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("incomplete bootstrap block", err)

    def test_missing_recipe_yaml_errors(self):
        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()  # no recipe.yaml inside
            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir), "22",
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("no recipe.yaml", err)

    def test_bad_argv_returns_2(self):
        rc, _, err = self._run_main(["fetch_bootstrap.py", "only-one-arg"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_pre_version_argv_form_rejected(self):
        """The old RECIPE_DIR OS ARCH [DOWNLOAD_DIR] form must not parse.

        Four arguments used to be the full call. Under the current
        signature they are RECIPE_DIR VERSION OS ARCH minus the arch,
        so a caller left un-migrated has to fail here rather than
        silently treat the os slug as the version.
        """
        rc, _, err = self._run_main(
            ["fetch_bootstrap.py", "rec", "ubuntu-24.04", "x86_64"],
        )
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
                ["fetch_bootstrap.py", str(recipe_dir), "22",
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
                bin_dir = Path(out_dir) / "install" / "bin"
                bin_dir.mkdir(parents=True)
                (bin_dir / "clang").write_text("#!/bin/false\n")
                (bin_dir / "clang").chmod(0o755)
            mock_dl.side_effect = _materialise

            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir), "22",
                 "ubuntu-24.04", "x86_64", str(download_dir)],
            )
            self.assertEqual(rc, 0, msg=err)
            # main() resolves the download_dir before printing, so the
            # expected path needs the same resolution to compare on
            # macOS where /var symlinks to /private/var.
            expected_bin = (
                download_dir.resolve() / "install" / "bin"
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
    def test_templated_version_reaches_compute_key(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        """`version: '{version}'` keys off the cell being built.

        The failure this guards is silent rather than loud: an
        unsubstituted '{version}' still produces a well-formed cache
        key, so the publish would fetch-miss on a cell nobody has ever
        published instead of naming the real provider.
        """
        mock_resolve.return_value = "file:///fake/cache"
        mock_probe.return_value = True
        mock_run.return_value = mock.Mock(stdout="key=fake-key\n")

        def _materialise(base, key, out_dir):
            bin_dir = Path(out_dir) / "install" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "clang").write_text("#!/bin/false\n")
            (bin_dir / "clang").chmod(0o755)
        mock_dl.side_effect = _materialise

        with tempfile.TemporaryDirectory() as d:
            recipe_dir = Path(d) / "rec"
            recipe_dir.mkdir()
            _write_recipe_yaml(recipe_dir, with_bootstrap=True,
                               bootstrap_version="{version}")
            rc, _, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir), "23",
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 0, msg=err)
            args = mock_run.call_args.args[0]
            self.assertIn("23", args)
            self.assertNotIn("{version}", args)

    @mock.patch("fetch_bootstrap.subprocess.run")
    @mock.patch("fetch_bootstrap.cache_io.cache_download")
    @mock.patch("fetch_bootstrap.cache_io.cache_probe")
    @mock.patch("fetch_bootstrap.cache_io.resolve_cache_base")
    def test_default_download_dir_is_recipe_sibling(
        self, mock_resolve, mock_probe, mock_dl, mock_run,
    ):
        """No-DOWNLOAD_DIR form: download lands at <recipe_dir>/_bootstrap.

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
                bin_dir = Path(out_dir) / "install" / "bin"
                bin_dir.mkdir(parents=True)
                (bin_dir / "clang").write_text("#!/bin/false\n")
                (bin_dir / "clang").chmod(0o755)
            mock_dl.side_effect = _materialise

            rc, out, err = self._run_main(
                ["fetch_bootstrap.py", str(recipe_dir), "22",
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 0, msg=err)
            expected_bin = (
                (recipe_dir / "_bootstrap").resolve()
                / "install" / "bin"
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
                ["fetch_bootstrap.py", str(recipe_dir), "22",
                 "ubuntu-24.04", "x86_64"],
            )
            self.assertEqual(rc, 1)
            self.assertIn("cell layout changed", err)


if __name__ == "__main__":
    unittest.main()
