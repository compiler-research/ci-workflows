"""Unit tests for bin/repro (nektos/act wrapper for local CI-failure reproduction).

Pin the contract: act invocation shape per flag combination, the
post-run shell + cleanup path, the SIG_IGN-around-Popen behavior
that keeps Ctrl+C interrupting cmake-inside-container instead of
killing the docker child.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import io
import os
import signal as _signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPRO_PATH = Path(__file__).resolve().parent / "repro"


def _load_repro():
    loader = importlib.machinery.SourceFileLoader("repro", str(REPRO_PATH))
    spec = importlib.util.spec_from_loader("repro", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


repro = _load_repro()


def _ns(**kw) -> argparse.Namespace:
    defaults = dict(list=False, job=None, workflow=None, matrix=[],
                    shell=True, save_temps=False, dry_run=False,
                    passthrough=[])
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# build_act_command
# ---------------------------------------------------------------------------

class BuildActCommandTests(unittest.TestCase):
    def setUp(self):
        # Mock _require so we don't need act on PATH for these tests.
        self._require_patch = mock.patch.object(
            repro, "_require", side_effect=lambda x: x)
        self._require_patch.start()

    def tearDown(self):
        self._require_patch.stop()

    def test_minimal_job_invocation_includes_reuse(self):
        cmd = repro.build_act_command(_ns(job="test"))
        self.assertEqual(cmd[:3], ["act", "-j", "test"])
        self.assertIn("--reuse", cmd)

    def test_no_shell_drops_reuse(self):
        cmd = repro.build_act_command(_ns(job="test", shell=False))
        self.assertEqual(cmd[:3], ["act", "-j", "test"])
        self.assertNotIn("--reuse", cmd)

    def test_workflow_passes_dash_W(self):
        cmd = repro.build_act_command(
            _ns(job="test", workflow="ci.yml", shell=False))
        self.assertEqual(cmd[:5], ["act", "-j", "test", "-W", "ci.yml"])

    def test_passthrough_after_dashdash(self):
        cmd = repro.build_act_command(
            _ns(job="test", shell=False,
                passthrough=["--", "--verbose", "--dryrun"]))
        # Trailing `--` separator is consumed; rest is appended.
        self.assertEqual(cmd[-2:], ["--verbose", "--dryrun"])

    def test_matrix_passes_through_each_filter(self):
        cmd = repro.build_act_command(
            _ns(job="build", shell=False,
                matrix=["name:osx26-arm-clang", "python:3.12"]))
        # Each --matrix becomes its own act flag pair (positions are
        # interleaved with the default -P platform maps).
        pairs = list(zip(cmd, cmd[1:]))
        self.assertIn(("--matrix", "name:osx26-arm-clang"), pairs)
        self.assertIn(("--matrix", "python:3.12"), pairs)

    def test_matrix_translates_cell_aliases(self):
        # `recipe`/`version`/`arch` are cell-coordinate aliases for
        # the real matrix keys (use-recipe / recipe-version /
        # recipe-arch). The suggestion output uses the aliases; the
        # translation here keeps `act --matrix` matching real keys.
        cmd = repro.build_act_command(
            _ns(job="build", shell=False,
                matrix=["recipe:llvm-root", "version:ROOT-llvm20",
                        "os:ubuntu-24.04", "arch:x86_64"]))
        pairs = list(zip(cmd, cmd[1:]))
        self.assertIn(("--matrix", "use-recipe:llvm-root"), pairs)
        self.assertIn(("--matrix", "recipe-version:ROOT-llvm20"), pairs)
        self.assertIn(("--matrix", "os:ubuntu-24.04"), pairs)
        self.assertIn(("--matrix", "recipe-arch:x86_64"), pairs)

    def test_default_platforms_present_when_no_actrc(self):
        with mock.patch.object(repro, "_actrc_platforms",
                               return_value=set()):
            cmd = repro.build_act_command(_ns(job="test", shell=False))
        pairs = list(zip(cmd, cmd[1:]))
        self.assertIn(("-P",
                       "ubuntu-24.04=ghcr.io/catthehacker/ubuntu:act-24.04"),
                      pairs)

    def test_actrc_slug_skipped(self):
        # User has -P ubuntu-24.04=... in ~/.actrc; bin/repro must
        # NOT inject its own default for that slug, or the user's
        # mapping gets silently overridden.
        with mock.patch.object(repro, "_actrc_platforms",
                               return_value={"ubuntu-24.04"}):
            cmd = repro.build_act_command(_ns(job="test", shell=False))
        # Other defaults still present.
        pairs = list(zip(cmd, cmd[1:]))
        self.assertIn(("-P",
                       "ubuntu-22.04=ghcr.io/catthehacker/ubuntu:act-22.04"),
                      pairs)
        # ubuntu-24.04 NOT injected.
        self.assertFalse(
            any(p[0] == "-P" and p[1].startswith("ubuntu-24.04=")
                for p in pairs)
        )

    def test_save_temps_implies_act_reuse_even_without_shell(self):
        """--save-temps needs the container to outlive act, same as
        --shell. Without --reuse act would set autoremove=true and the
        container would be gone before save-temps could preserve it."""
        cmd = repro.build_act_command(_ns(job="test", shell=False,
                                          save_temps=True))
        self.assertIn("--reuse", cmd)

    def test_no_shell_no_save_temps_omits_act_reuse(self):
        """Default (act --rm) cleanup -- nothing for bin/repro to do
        afterwards."""
        cmd = repro.build_act_command(_ns(job="test", shell=False))
        self.assertNotIn("--reuse", cmd)

    def test_dry_run_passes_act_dash_n(self):
        cmd = repro.build_act_command(_ns(job="test", shell=False,
                                          dry_run=True))
        self.assertIn("-n", cmd)

    def test_dry_run_omits_act_reuse_even_with_shell(self):
        """Dry-run never creates a container, so --reuse is moot.
        We still pass --reuse here (build_act_command honors --shell
        regardless), but main() short-circuits before any docker
        interaction. The shape stays consistent."""
        cmd = repro.build_act_command(_ns(job="test", shell=True,
                                          dry_run=True))
        # --reuse stays since --shell is True; main() handles the
        # actual short-circuit. Just pin -n is present.
        self.assertIn("-n", cmd)

    def test_arch_matrix_differing_from_host_passes_container_architecture(self):
        """Apple Silicon host (arm64), x86_64 cell: bin/repro must
        force docker to run the runner image as linux/amd64,
        otherwise docker pulls the arm64 variant by default and
        x86_64 LLVM binaries fail inside."""
        with mock.patch.object(repro, "host_arch", return_value="arm64"):
            cmd = repro.build_act_command(_ns(
                job="test", shell=False, matrix=["arch:x86_64"]))
        self.assertIn("--container-architecture", cmd)
        i = cmd.index("--container-architecture")
        self.assertEqual(cmd[i + 1], "linux/amd64")

    def test_arch_matrix_matching_host_omits_container_architecture(self):
        """Host arch matches cell: act's default (host arch) is
        already correct; passing --container-architecture would be
        redundant."""
        with mock.patch.object(repro, "host_arch", return_value="x86_64"):
            cmd = repro.build_act_command(_ns(
                job="test", shell=False, matrix=["arch:x86_64"]))
        self.assertNotIn("--container-architecture", cmd)

    def test_no_arch_matrix_omits_container_architecture(self):
        """No -m arch:<X>, no -m os:<X>, no -m name:<X> -> we don't
        know the cell's arch -> let act use its default. Don't
        second-guess."""
        with mock.patch.object(repro, "host_arch", return_value="arm64"):
            cmd = repro.build_act_command(_ns(
                job="test", shell=False, matrix=["recipe:llvm-asan"]))
        self.assertNotIn("--container-architecture", cmd)

    def test_os_matrix_derives_container_architecture(self):
        """`-m os:ubuntu-24.04` on an arm64 host: the slug uniquely
        implies x86_64, so bin/repro should set linux/amd64 even
        though the user didn't say `arch:`."""
        with mock.patch.object(repro, "host_arch", return_value="arm64"):
            cmd = repro.build_act_command(_ns(
                job="test", shell=False, matrix=["os:ubuntu-24.04"]))
        self.assertIn("--container-architecture", cmd)
        i = cmd.index("--container-architecture")
        self.assertEqual(cmd[i + 1], "linux/amd64")

    def test_name_matrix_resolves_arch_via_dryrun_lookup(self):
        """`-m name:<row>`: bin/repro looks the row up in the
        act -n --json data and reads `os` from the matrix dict."""
        rows = [{"jobID": "build",
                 "matrix": {"name": "row-x86", "os": "ubuntu-24.04"},
                 "workflow_name": "CI", "row_name": "row-x86"}]
        with mock.patch.object(repro, "host_arch", return_value="arm64"), \
             mock.patch.object(repro, "_act_dryrun_rows", return_value=rows):
            cmd = repro.build_act_command(_ns(
                job="test", shell=False, matrix=["name:row-x86"]))
        i = cmd.index("--container-architecture")
        self.assertEqual(cmd[i + 1], "linux/amd64")


# ---------------------------------------------------------------------------
# Container discovery + cleanup
# ---------------------------------------------------------------------------

class FindRecentActContainerTests(unittest.TestCase):
    def test_returns_id_when_present(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="abc123def\n", returncode=0)
            self.assertEqual(repro.find_recent_act_container(), "abc123def")

    def test_returns_none_when_empty(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="\n", returncode=0)
            self.assertIsNone(repro.find_recent_act_container())

    def test_filter_args_target_act_containers(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="", returncode=0)
            repro.find_recent_act_container()
            argv = run.call_args[0][0]
            self.assertIn("--filter", argv)
            self.assertIn("name=act-", argv)
            self.assertIn("--latest", argv)


class RemoveContainerTests(unittest.TestCase):
    def test_invokes_docker_rm_dash_f(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            repro.remove_container("abc123")
            run.assert_called_once()
            argv = run.call_args[0][0]
            self.assertEqual(argv, ["docker", "rm", "-f", "abc123"])


# ---------------------------------------------------------------------------
# _confirm: y/n prompt with TTY fallback
# ---------------------------------------------------------------------------

class PublishedCellsTests(unittest.TestCase):
    """published_cells parses cells.yaml into a list of dicts. Inputs
    are well-defined (one inline-flow YAML row per cell), so a
    hand-rolled parser is enough."""

    def test_parses_repo_cells_yaml_to_nonempty_list(self):
        # Sanity check against the real cells.yaml at repo root.
        cells = repro.published_cells()
        self.assertGreater(len(cells), 0)
        for c in cells:
            for k in ("recipe", "version", "os", "arch"):
                self.assertIn(k, c)

    def test_missing_file_returns_empty_list(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            self.assertEqual(
                repro.published_cells(Path(d) / "nope.yaml"), [])

    def test_strips_quotes_around_values(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from textwrap import dedent
        cells_text = dedent("""\
            cells:
              - { recipe: llvm-asan, version: '22', os: ubuntu-24.04, arch: x86_64 }
              - { recipe: llvm-root, version: "ROOT-llvm20", os: macos-26, arch: arm64 }
            """)
        with TemporaryDirectory() as d:
            p = Path(d) / "cells.yaml"
            p.write_text(cells_text)
            cells = repro.published_cells(p)
        self.assertEqual(len(cells), 2)
        self.assertEqual(cells[0]["version"], "22")        # single-quotes stripped
        self.assertEqual(cells[1]["version"], "ROOT-llvm20")  # double-quotes stripped

    def test_stops_at_next_top_level_key(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from textwrap import dedent
        cells_text = dedent("""\
            cells:
              - { recipe: llvm-asan, version: '22', os: ubuntu-24.04, arch: x86_64 }
            other_key:
              foo: bar
            """)
        with TemporaryDirectory() as d:
            p = Path(d) / "cells.yaml"
            p.write_text(cells_text)
            cells = repro.published_cells(p)
        self.assertEqual(len(cells), 1)


class ResolveWorkflowTests(unittest.TestCase):
    """`-W foo.yml` from the user's checkout typically means "the
    foo.yml in this repo's .github/workflows/", not a literal
    cwd-relative path. Pin the resolution behaviour."""

    def test_passthrough_when_path_exists_as_given(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            literal = Path(d) / "explicit.yml"
            literal.write_text("name: t\n")
            self.assertEqual(repro.resolve_workflow(str(literal)),
                             str(literal))

    def test_bare_basename_expands_to_github_workflows(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            wfdir = Path(d) / ".github" / "workflows"
            wfdir.mkdir(parents=True)
            (wfdir / "main.yml").write_text("name: t\n")
            old = os.getcwd()
            try:
                os.chdir(d)
                # Compare via Path.as_posix() so the test passes on
                # Windows (where str(Path('a/b')) yields 'a\\b').
                self.assertEqual(
                    Path(repro.resolve_workflow("main.yml")).as_posix(),
                    ".github/workflows/main.yml")
            finally:
                os.chdir(old)

    def test_neither_path_passes_through_for_act_to_complain(self):
        # If both forms miss, return the original; act will emit
        # the clearer "no such file" message itself.
        self.assertEqual(repro.resolve_workflow("nope.yml"), "nope.yml")

    def test_none_passes_through(self):
        self.assertIsNone(repro.resolve_workflow(None))


class CheckJobAmbiguousTests(unittest.TestCase):
    """Pin the load-bearing pre-flight: bin/repro must refuse to
    proceed when -j matches multiple workflow files and -W wasn't
    given. Without this, the user's actual `bin/repro -j build`
    invocation ran build jobs from five different workflows in
    parallel."""

    DUP_LISTING = [
        {"stage": "0", "job_id": "build", "job_name": "build",
         "workflow_name": "Markdown-Linter",
         "workflow_file": "markdown-linter.yml"},
        {"stage": "1", "job_id": "build", "job_name": "${{ matrix.name }}",
         "workflow_name": "Native Builds",
         "workflow_file": "main.yml"},
    ]

    def test_ambiguous_without_workflow_exits(self):
        ns = argparse.Namespace(job="build", workflow=None)
        with mock.patch.object(repro, "_act_listing",
                               return_value=self.DUP_LISTING):
            with self.assertRaises(SystemExit) as cm:
                repro.check_job_ambiguous(ns)
        msg = str(cm.exception)
        self.assertIn("multiple workflows", msg)
        self.assertIn("markdown-linter.yml", msg)
        self.assertIn("main.yml", msg)
        self.assertIn("-W ", msg)  # copy-pasteable hint

    def test_with_workflow_short_circuits(self):
        ns = argparse.Namespace(job="build", workflow="main.yml")
        # Even if act_listing would say ambiguous, we trust the user.
        with mock.patch.object(repro, "_act_listing",
                               return_value=self.DUP_LISTING):
            repro.check_job_ambiguous(ns)  # must not raise

    def test_unique_job_id_does_not_exit(self):
        ns = argparse.Namespace(job="precheckin", workflow=None)
        with mock.patch.object(repro, "_act_listing",
                               return_value=[
                                   {"stage": "0", "job_id": "precheckin",
                                    "job_name": "precheckin",
                                    "workflow_name": "clang-format",
                                    "workflow_file": "clang-format.yml"}]):
            repro.check_job_ambiguous(ns)  # must not raise

    def test_listing_failure_does_not_exit(self):
        """If act -l can't be parsed, fall through to act and let act
        complain naturally rather than blocking."""
        ns = argparse.Namespace(job="build", workflow=None)
        with mock.patch.object(repro, "_act_listing", return_value=[]):
            repro.check_job_ambiguous(ns)  # no-op


class HostArchTests(unittest.TestCase):
    """Pin the Rosetta-aware host detection so the 'most efficient
    cell' suggestion doesn't silently regress Apple Silicon users to
    'host is x86_64' on x86_64 Pythons."""

    def test_darwin_arm64_via_sysctl_even_under_rosetta(self):
        with mock.patch.object(repro.platform, "system", return_value="Darwin"), \
             mock.patch.object(repro.platform, "machine", return_value="x86_64"), \
             mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="1\n", returncode=0)
            self.assertEqual(repro.host_arch(), "arm64")

    def test_darwin_intel_when_sysctl_returns_zero(self):
        with mock.patch.object(repro.platform, "system", return_value="Darwin"), \
             mock.patch.object(repro.platform, "machine", return_value="x86_64"), \
             mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="0\n", returncode=0)
            self.assertEqual(repro.host_arch(), "x86_64")

    def test_linux_aarch64_normalized_to_arm64(self):
        with mock.patch.object(repro.platform, "system", return_value="Linux"), \
             mock.patch.object(repro.platform, "machine", return_value="aarch64"):
            self.assertEqual(repro.host_arch(), "arm64")


class ListJobsCellHintTests(unittest.TestCase):
    """After `act -l`, list_jobs prints the published cells from
    cells.yaml so the user can pick a matrix row that hits the
    download-fast path instead of building inline."""

    def test_hint_emits_minimal_repro_for_unique_row_name(self):
        # A row whose name is unique across workflows gets the bare
        # `bin/repro -m name:<row>` -- no -W / -j needed; main()
        # auto-detects them.
        fake_cells = [{"recipe": "llvm-asan", "version": "22",
                       "os": "ubuntu-24.04", "arch": "x86_64"}]
        fake_rows = [("main.yml", "build",
                      "ubu24-x86-clang22-llvm22-asan-ubsan")]
        fake_dryrun = [{
            "jobID": "build",
            "matrix": {"name": "ubu24-x86-clang22-llvm22-asan-ubsan",
                       "use-recipe": "llvm-asan",
                       "clang-runtime": "22",
                       "os": "ubuntu-24.04"},
            "workflow_name": "CI",
            "row_name": "ubu24-x86-clang22-llvm22-asan-ubsan",
        }]
        with mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen") as popen, \
             mock.patch.object(repro, "published_cells",
                               return_value=fake_cells), \
             mock.patch.object(repro, "discover_matrix_rows",
                               return_value=fake_rows), \
             mock.patch.object(repro, "_act_dryrun_rows",
                               return_value=fake_dryrun), \
             mock.patch.object(repro, "_failed_rows_for_branch",
                               return_value=set()), \
             mock.patch.object(repro, "host_arch", return_value="x86_64"):
            popen.return_value.wait = lambda: 0
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.list_jobs(_ns(list=True))
        out = buf.getvalue()
        self.assertIn(
            "bin/repro ubu24-x86-clang22-llvm22-asan-ubsan",
            out)
        self.assertIn(
            "[cell: llvm-asan/22/ubuntu-24.04/x86_64]", out)
        self.assertNotIn("-W main.yml", out)

    def test_hint_full_form_when_row_name_collides(self):
        # Same row name across two workflows: the minimal form is
        # ambiguous, so emit the full -W / -j form for each.
        fake_rows = [
            ("main.yml", "build", "shared-row-name"),
            ("other.yml", "build", "shared-row-name"),
        ]
        with mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen") as popen, \
             mock.patch.object(repro, "published_cells", return_value=[]), \
             mock.patch.object(repro, "discover_matrix_rows",
                               return_value=fake_rows), \
             mock.patch.object(repro, "_failed_rows_for_branch",
                               return_value=set()), \
             mock.patch.object(repro, "host_arch", return_value="x86_64"):
            popen.return_value.wait = lambda: 0
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.list_jobs(_ns(list=True))
        out = buf.getvalue()
        self.assertIn(
            "bin/repro -W main.yml -j build -m name:shared-row-name",
            out)
        self.assertIn(
            "bin/repro -W other.yml -j build -m name:shared-row-name",
            out)


class FindWorkflowForCellMatrixTests(unittest.TestCase):
    """When the user's -m flags pin a recipe, only one workflow in
    .github/workflows/ typically consumes that recipe. bin/repro
    auto-picks -W so the user doesn't have to type it."""

    def test_unique_workflow_matched_by_recipe(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from textwrap import dedent
        with TemporaryDirectory() as d:
            base = Path(d)
            wfdir = base / ".github" / "workflows"
            wfdir.mkdir(parents=True)
            # Workflow A: setup-recipe with llvm-asan -> match.
            (wfdir / "ci.yml").write_text(dedent("""\
                jobs:
                  build:
                    runs-on: ubuntu-24.04
                    steps:
                      - uses: compiler-research/ci-workflows/actions/setup-recipe@main
                        with:
                          recipe: llvm-asan
                """))
            # Workflow B: setup-recipe but for a different recipe.
            (wfdir / "root.yml").write_text(dedent("""\
                jobs:
                  build:
                    runs-on: ubuntu-24.04
                    steps:
                      - uses: compiler-research/ci-workflows/actions/setup-recipe@main
                        with:
                          recipe: llvm-root
                """))
            wf, job = repro.find_workflow_for_cell_matrix(
                ["recipe:llvm-asan", "version:22"], cwd=base)
        # Path.as_posix() so the test passes on Windows (str(Path('a/b'))
        # yields 'a\\b').
        self.assertEqual(Path(wf).as_posix(), ".github/workflows/ci.yml")
        self.assertEqual(job, "build")

    def test_no_recipe_in_matrix_returns_none(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            self.assertIsNone(
                repro.find_workflow_for_cell_matrix([], cwd=Path(d)))
            self.assertIsNone(
                repro.find_workflow_for_cell_matrix(
                    ["arch:x86_64"], cwd=Path(d)))

    def test_multiple_matches_returns_none(self):
        """Two workflows both setup-recipe llvm-asan: ambiguous,
        let check_job_ambiguous report it instead of guessing."""
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from textwrap import dedent
        with TemporaryDirectory() as d:
            base = Path(d)
            wfdir = base / ".github" / "workflows"
            wfdir.mkdir(parents=True)
            for name in ("ci.yml", "release.yml"):
                (wfdir / name).write_text(dedent("""\
                    jobs:
                      build:
                        steps:
                          - uses: compiler-research/ci-workflows/actions/setup-recipe@main
                            with:
                              recipe: llvm-asan
                    """))
            self.assertIsNone(repro.find_workflow_for_cell_matrix(
                ["recipe:llvm-asan"], cwd=base))

    def test_no_workflows_dir_returns_none(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as d:
            self.assertIsNone(repro.find_workflow_for_cell_matrix(
                ["recipe:llvm-asan"], cwd=Path(d)))


class FindWorkflowViaDryRunTests(unittest.TestCase):
    """Disambiguation fallback: when text-grep can't pick a unique
    workflow, run `act -n -W <each>` per candidate and keep only
    those whose dry-run accepts the matrix filter."""

    LISTING = [
        {"stage": "0", "job_id": "build", "job_name": "Linter",
         "workflow_name": "Markdown-Linter",
         "workflow_file": "markdown-linter.yml"},
        {"stage": "0", "job_id": "build", "job_name": "${{ matrix.name }}",
         "workflow_name": "Native Builds",
         "workflow_file": "main.yml"},
    ]

    def test_unique_dry_run_match_returned(self):
        """Two workflows have job=build; only main.yml's dry-run
        emits a Run-Set-up-job marker (matches our matrix filter).
        Pick main.yml."""
        def fake_run(cmd, **kw):
            wf = cmd[cmd.index("-W") + 1]
            if "main.yml" in wf:
                return mock.Mock(returncode=0,
                                 stdout="[Native Builds/build] ⭐ Run Set up job\n")
            return mock.Mock(returncode=0, stdout="")  # no rows matched

        with mock.patch.object(repro, "_act_listing",
                               return_value=self.LISTING), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_run), \
             mock.patch.object(repro, "_require",
                               side_effect=lambda x: x), \
             mock.patch.object(repro, "resolve_workflow",
                               side_effect=lambda x: x), \
             mock.patch("pathlib.Path.is_file", return_value=True):
            r = repro.find_workflow_via_dry_run(
                _ns(job="build", matrix=["recipe:llvm-root"]))
        self.assertEqual(r, ("main.yml", "build"))

    def test_multiple_dry_run_matches_returns_none(self):
        """Both workflows accept the filter -- still ambiguous, defer
        to check_job_ambiguous's user-facing error."""
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=0, stdout="⭐ Run Set up job\n")
        with mock.patch.object(repro, "_act_listing",
                               return_value=self.LISTING), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_run), \
             mock.patch.object(repro, "_require",
                               side_effect=lambda x: x), \
             mock.patch.object(repro, "resolve_workflow",
                               side_effect=lambda x: x), \
             mock.patch("pathlib.Path.is_file", return_value=True):
            self.assertIsNone(repro.find_workflow_via_dry_run(
                _ns(job="build", matrix=["recipe:llvm-root"])))

    def test_empty_listing_returns_none(self):
        with mock.patch.object(repro, "_act_listing", return_value=[]):
            self.assertIsNone(repro.find_workflow_via_dry_run(
                _ns(job="build", matrix=["recipe:llvm-root"])))


class ConfirmTests(unittest.TestCase):
    def test_no_tty_returns_default(self):
        with mock.patch.object(repro.sys.stdin, "isatty", return_value=False):
            self.assertTrue(repro._confirm("?", default=True))
            self.assertFalse(repro._confirm("?", default=False))

    def test_bare_enter_picks_default(self):
        with mock.patch.object(repro.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(repro.sys.stdin, "readline", return_value="\n"):
            self.assertTrue(repro._confirm("?", default=True))
            self.assertFalse(repro._confirm("?", default=False))

    def test_yes_responses(self):
        for resp in ("y\n", "Y\n", "yes\n", "YES\n", "Yes\n"):
            with mock.patch.object(repro.sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(repro.sys.stdin, "readline", return_value=resp):
                self.assertTrue(repro._confirm("?", default=False),
                                msg=f"expected True for {resp!r}")

    def test_no_response_falls_through_to_false_when_default_true(self):
        with mock.patch.object(repro.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(repro.sys.stdin, "readline", return_value="n\n"):
            self.assertFalse(repro._confirm("?", default=True))

    def test_eof_returns_default(self):
        """Ctrl+D / closed stdin during prompt: don't hang, use default."""
        with mock.patch.object(repro.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(repro.sys.stdin, "readline",
                               side_effect=EOFError):
            self.assertTrue(repro._confirm("?", default=True))


# ---------------------------------------------------------------------------
# In-container patch export
# ---------------------------------------------------------------------------

class ContainerTreeDirtyTests(unittest.TestCase):
    def test_clean_returns_false(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertFalse(repro._container_tree_dirty("cid", "/ws"))
        # Sanity: invocation shape.
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["docker", "exec", "cid"])
        self.assertIn("--quiet", argv)
        self.assertIn("HEAD", argv)

    def test_diff_present_returns_true(self):
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            self.assertTrue(repro._container_tree_dirty("cid", "/ws"))

    def test_not_a_git_repo_returns_false(self):
        """rc=128 (no HEAD, no .git, git missing) is not actionable
        here -- the user can always docker exec themselves; we'd just
        be noisy. Behave as 'nothing to export'."""
        with mock.patch.object(repro.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=128, stdout="",
                                          stderr="not a git repo")
            self.assertFalse(repro._container_tree_dirty("cid", "/ws"))


class MaybeExportInContainerPatchTests(unittest.TestCase):
    def test_clean_tree_skips_prompt(self):
        with mock.patch.object(repro, "_container_tree_dirty",
                               return_value=False), \
             mock.patch.object(repro, "_confirm") as conf:
            repro.maybe_export_in_container_patch("cid", "row1")
        conf.assert_not_called()

    def test_dirty_tree_prompts_default_yes(self):
        """Default Y -- losing in-container edits is the worse
        failure mode."""
        with mock.patch.object(repro, "_container_tree_dirty",
                               return_value=True), \
             mock.patch.object(repro, "_confirm",
                               return_value=False) as conf, \
             mock.patch.object(repro, "_export_container_patch") as exp:
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.maybe_export_in_container_patch("cid", "row1")
        conf.assert_called_once()
        self.assertEqual(conf.call_args.kwargs.get("default"), True)
        # User declined -> no export.
        exp.assert_not_called()

    def test_user_confirms_export_writes_patch_and_logs_apply_hint(self):
        with mock.patch.object(repro, "_container_tree_dirty",
                               return_value=True), \
             mock.patch.object(repro, "_confirm", return_value=True), \
             mock.patch.object(repro, "_export_container_patch",
                               return_value=True) as exp:
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.maybe_export_in_container_patch("cid", "row1")
        # Wrote to /tmp/repro-<row>.patch.
        args = exp.call_args[0]
        self.assertEqual(args[0], "cid")
        # Path.as_posix() so the test passes on Windows.
        self.assertEqual(args[2].as_posix(), "/tmp/repro-row1.patch")
        self.assertIn("git apply", buf.getvalue())

    def test_no_row_falls_back_to_session_name(self):
        with mock.patch.object(repro, "_container_tree_dirty",
                               return_value=True), \
             mock.patch.object(repro, "_confirm", return_value=True), \
             mock.patch.object(repro, "_export_container_patch",
                               return_value=True) as exp:
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.maybe_export_in_container_patch("cid", None)
        # Path.as_posix() so the test passes on Windows.
        self.assertEqual(exp.call_args[0][2].as_posix(),
                         "/tmp/repro-session.patch")

    def test_export_failure_does_not_log_apply_hint(self):
        """If the patch write fails, don't tell the user to apply
        a file that doesn't exist."""
        with mock.patch.object(repro, "_container_tree_dirty",
                               return_value=True), \
             mock.patch.object(repro, "_confirm", return_value=True), \
             mock.patch.object(repro, "_export_container_patch",
                               return_value=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.maybe_export_in_container_patch("cid", "row1")
        self.assertNotIn("git apply", buf.getvalue())


# ---------------------------------------------------------------------------
# Workspace-clash pre-flight
# ---------------------------------------------------------------------------

class WorkspaceClashTests(unittest.TestCase):
    def _ns_clash(self, **kw):
        # MainDispatch fields are a superset; warn_workspace_clashes
        # only reads dry_run and skip_clash_check.
        defaults = dict(dry_run=False, skip_clash_check=False)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_no_clash_no_warning(self):
        with mock.patch.object(repro, "detect_workspace_clashes",
                               return_value=[]), \
             mock.patch.object(repro, "_confirm") as conf:
            repro.warn_workspace_clashes(self._ns_clash())
        conf.assert_not_called()

    def test_clash_prompts_and_exits_on_decline(self):
        """Default answer is 'no' (don't proceed). Bare-Enter at the
        prompt -> SystemExit(1). The user lost no data; they get to
        clean up and re-run."""
        with mock.patch.object(repro, "detect_workspace_clashes",
                               return_value=[Path("/tmp/build")]), \
             mock.patch.object(repro, "_confirm",
                               return_value=False) as conf:
            buf = io.StringIO()
            with redirect_stderr(buf), \
                 self.assertRaises(SystemExit) as cm:
                repro.warn_workspace_clashes(self._ns_clash())
        self.assertEqual(cm.exception.code, 1)
        # Prompt fired with default=False (the safer choice).
        conf.assert_called_once()
        self.assertEqual(conf.call_args.kwargs.get("default"), False)
        # Listed paths are surfaced in the warning. The code logs
        # via str(Path), so use the same form here -- str(Path('/tmp/
        # build')) is '/tmp/build' on POSIX and '\\tmp\\build' on
        # Windows.
        self.assertIn(str(Path("/tmp/build")), buf.getvalue())

    def test_clash_proceeds_when_user_confirms(self):
        with mock.patch.object(repro, "detect_workspace_clashes",
                               return_value=[Path("/tmp/build")]), \
             mock.patch.object(repro, "_confirm", return_value=True):
            buf = io.StringIO()
            with redirect_stderr(buf):
                # Should not raise.
                repro.warn_workspace_clashes(self._ns_clash())

    def test_skip_flag_bypasses_check_entirely(self):
        with mock.patch.object(repro, "detect_workspace_clashes") as det, \
             mock.patch.object(repro, "_confirm") as conf:
            repro.warn_workspace_clashes(
                self._ns_clash(skip_clash_check=True))
        det.assert_not_called()
        conf.assert_not_called()

    def test_dry_run_bypasses_check(self):
        """--dry-run: act doesn't actually mkdir anything; the clash
        is only relevant for real runs."""
        with mock.patch.object(repro, "detect_workspace_clashes") as det, \
             mock.patch.object(repro, "_confirm") as conf:
            repro.warn_workspace_clashes(self._ns_clash(dry_run=True))
        det.assert_not_called()
        conf.assert_not_called()


# ---------------------------------------------------------------------------
# SIGINT handling
# ---------------------------------------------------------------------------

class RunInteractiveTests(unittest.TestCase):
    def test_sigint_ignored_during_run_then_restored(self):
        observed = []

        def fake_popen(_cmd):
            class _Proc:
                def wait(self_):
                    observed.append(_signal.getsignal(_signal.SIGINT))
                    return 0
            return _Proc()

        sentinel = lambda *a: None
        prev = _signal.signal(_signal.SIGINT, sentinel)
        try:
            with mock.patch.object(repro.subprocess, "Popen",
                                   side_effect=fake_popen):
                rc = repro._run_interactive(["act"])
            self.assertEqual(rc, 0)
            self.assertEqual(observed, [_signal.SIG_IGN])
            self.assertEqual(_signal.getsignal(_signal.SIGINT), sentinel)
        finally:
            _signal.signal(_signal.SIGINT, prev)

    def test_handler_restored_on_subprocess_exception(self):
        def fake_popen(_cmd):
            class _Proc:
                def wait(self_):
                    raise OSError("simulated")
            return _Proc()

        sentinel = lambda *a: None
        prev = _signal.signal(_signal.SIGINT, sentinel)
        try:
            with mock.patch.object(repro.subprocess, "Popen",
                                   side_effect=fake_popen):
                with self.assertRaises(OSError):
                    repro._run_interactive(["act"])
            self.assertEqual(_signal.getsignal(_signal.SIGINT), sentinel)
        finally:
            _signal.signal(_signal.SIGINT, prev)


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------

class MainDispatchTests(unittest.TestCase):
    def test_list_short_circuits_to_act_dash_l(self):
        argv = ["bin/repro", "--list"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen") as popen, \
             mock.patch.object(repro, "host_arch", return_value="x86_64"):
            popen.return_value = mock.Mock()
            popen.return_value.wait = lambda: 0
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 0)
        # First Popen call is `act -l` (before _print_fast_cells_hint).
        cmd = popen.call_args_list[0][0][0]
        self.assertEqual(cmd, ["act", "-l"])

    def test_no_job_no_list_exits(self):
        argv = ["bin/repro"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x):
            with self.assertRaises(SystemExit) as cm:
                repro.main()
        self.assertIn("-j NAME, --list, or -m name:", str(cm.exception))

    def test_job_default_runs_shell_then_prompts_remove(self):
        """Default flow: after act exits, drop into shell, then PROMPT
        whether to remove the container. Bare-Enter (default=Y) -->
        rm. Pin both that the prompt fires AND that 'yes' triggers
        removal -- the 0-disk path's load-bearing assertion."""
        invocations = []

        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 0)

        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            if cmd[:3] == ["docker", "ps", "-aq"]:
                return mock.Mock(stdout="abc123def\n", returncode=0)
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run), \
             mock.patch.object(repro, "_confirm", return_value=True) as conf:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 0)
        # Prompt was issued.
        conf.assert_called_once()
        self.assertIn("remove container", conf.call_args[0][0])

        # 1. act ran with --reuse.
        act_cmds = [c for kind, c in invocations if kind == "popen"
                    and c[0] == "act"]
        self.assertEqual(len(act_cmds), 1)
        self.assertIn("--reuse", act_cmds[0])

        # 2. docker exec into the discovered container.
        exec_cmds = [c for kind, c in invocations if kind == "popen"
                     and c[0] == "docker" and c[1] == "exec"]
        self.assertEqual(len(exec_cmds), 1)
        self.assertEqual(exec_cmds[0],
                         ["docker", "exec", "-it", "abc123def", "bash"])

        # 3. docker rm fired (user said yes at the prompt).
        rm_calls = [c for kind, c in invocations if kind == "run"
                    and c[:3] == ["docker", "rm", "-f"]]
        self.assertEqual(len(rm_calls), 1)

    def test_user_declines_remove_keeps_container_logs_reentry(self):
        """User answers 'n' at the prompt: container preserved and
        the re-entry hint is logged."""
        invocations = []
        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 0)
        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            if cmd[:3] == ["docker", "ps", "-aq"]:
                return mock.Mock(stdout="abc123def\n", returncode=0)
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run), \
             mock.patch.object(repro, "_confirm", return_value=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.main()
        # No `docker rm -f` because user said no.
        rm_calls = [c for kind, c in invocations if kind == "run"
                    and c[:3] == ["docker", "rm", "-f"]]
        self.assertEqual(rm_calls, [])
        # Re-entry hint logged.
        out = buf.getvalue()
        self.assertIn("preserved", out)
        self.assertIn("docker exec", out)
        self.assertIn("abc123def", out)

    def test_save_temps_skips_prompt(self):
        """--save-temps is the user pre-answering 'no' before the
        shell starts; no prompt should fire."""
        with mock.patch.object(sys, "argv",
                               ["bin/repro", "-j", "test", "--save-temps"]), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen") as popen, \
             mock.patch.object(repro.subprocess, "run") as run, \
             mock.patch.object(repro, "_confirm") as conf:
            popen.return_value.wait = lambda: 0
            run.side_effect = lambda cmd, **kw: (
                mock.Mock(stdout="abc123def\n", returncode=0)
                if cmd[:3] == ["docker", "ps", "-aq"]
                else mock.Mock(stdout="", returncode=0))
            buf = io.StringIO()
            with redirect_stderr(buf):
                repro.main()
        conf.assert_not_called()

    def test_no_shell_no_save_temps_does_nothing_post_run(self):
        """act with autoremove=true cleans up its own container; the
        script just propagates the exit code."""
        invocations = []
        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 7)
        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test", "--no-shell"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run):
            rc = repro.main()
        self.assertEqual(rc, 7)
        # No docker exec, no docker rm. act's --reuse omitted.
        for kind, cmd in invocations:
            self.assertNotIn("exec", cmd)
            self.assertNotIn("rm", cmd)
            if kind == "popen" and cmd[0] == "act":
                self.assertNotIn("--reuse", cmd)

    def test_dry_run_skips_shell_and_cleanup(self):
        """--dry-run never creates a container; any post-run docker
        interaction would emit misleading output. main() must
        short-circuit before any 'no act-* container found' message."""
        invocations = []
        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 0)
        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test", "--dry-run"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 0)
        # Only act ran; no docker ps/exec/rm invocations.
        for kind, cmd in invocations:
            self.assertNotEqual(cmd[:2], ["docker", "ps"])
            self.assertNotIn("exec", cmd)
            self.assertNotIn("rm", cmd)
        # The misleading "no act-* container found" message must
        # NOT have been logged.
        self.assertNotIn("no act-* container", buf.getvalue())

    def test_save_temps_keeps_container_and_logs_reentry(self):
        invocations = []
        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 0)
        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            if cmd[:3] == ["docker", "ps", "-aq"]:
                return mock.Mock(stdout="abc123def\n", returncode=0)
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test", "--no-shell", "--save-temps"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 0)
        # No `docker rm -f` invocation.
        rm_calls = [c for kind, c in invocations
                    if kind == "run" and c[:3] == ["docker", "rm", "-f"]]
        self.assertEqual(rm_calls, [])
        # Re-entry hint logged.
        out = buf.getvalue()
        self.assertIn("--save-temps", out)
        self.assertIn("docker exec", out)
        self.assertIn("abc123def", out)

    def test_shell_plus_save_temps_runs_shell_then_keeps_container(self):
        """--shell --save-temps: drop into shell, but DON'T rm on
        shell exit. Useful for `enter, poke, leave, come back later`
        flows."""
        invocations = []
        def fake_popen(cmd):
            invocations.append(("popen", cmd))
            return mock.Mock(wait=lambda: 0)
        def fake_subprocess_run(cmd, **kw):
            invocations.append(("run", cmd[:3]))
            if cmd[:3] == ["docker", "ps", "-aq"]:
                return mock.Mock(stdout="abc123def\n", returncode=0)
            return mock.Mock(stdout="", returncode=0)

        argv = ["bin/repro", "-j", "test", "--save-temps"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 0)
        # Shell ran (docker exec via Popen).
        exec_calls = [c for kind, c in invocations
                      if kind == "popen" and c[0] == "docker"
                      and c[1] == "exec"]
        self.assertEqual(len(exec_calls), 1)
        # No `docker rm -f`.
        rm_calls = [c for kind, c in invocations
                    if kind == "run" and c[:3] == ["docker", "rm", "-f"]]
        self.assertEqual(rm_calls, [])

    def test_no_act_container_after_run_skips_shell(self):
        """If act failed before creating a container, find_recent_act
        returns None; we log and return act's rc rather than blowing
        up trying to docker exec into nothing."""
        def fake_popen(cmd):
            return mock.Mock(wait=lambda: 1)
        def fake_subprocess_run(cmd, **kw):
            return mock.Mock(stdout="\n", returncode=0)

        argv = ["bin/repro", "-j", "test"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(repro, "_require", side_effect=lambda x: x), \
             mock.patch.object(repro.subprocess, "Popen",
                               side_effect=fake_popen), \
             mock.patch.object(repro.subprocess, "run",
                               side_effect=fake_subprocess_run):
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = repro.main()
        self.assertEqual(rc, 1)
        self.assertIn("no act-* container found", buf.getvalue())


# ---------------------------------------------------------------------------
# Row-name pattern resolution
# ---------------------------------------------------------------------------

class ResolvePatternTests(unittest.TestCase):
    """`_resolve_pattern` is the row-name glob behind the
    `bin/repro <name>` shortcut. Verify that unique matches populate
    args.workflow/job/matrix correctly, ambiguous matches bail with
    a clear message, and a typo prints the available rows."""

    _ROWS = [
        ("main.yml", "build", "ubu24-x86-gcc14"),
        ("main.yml", "build", "ubu24-x86-clang22-asan"),
        ("main.yml", "build", "osx26-arm-clang"),
    ]

    def _ns(self, **kw):
        defaults = dict(workflow=None, job=None, matrix=None)
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_unique_match_populates_args(self):
        args = self._ns()
        with mock.patch.object(repro, "discover_matrix_rows",
                               return_value=self._ROWS):
            repro._resolve_pattern("ubu24-x86-gcc14", args)
        self.assertEqual(args.workflow, "main.yml")
        self.assertEqual(args.job, "build")
        self.assertEqual(args.matrix, ["name:ubu24-x86-gcc14"])

    def test_existing_workflow_not_overwritten(self):
        """If the user already passed -W, don't second-guess them."""
        args = self._ns(workflow="explicit.yml")
        with mock.patch.object(repro, "discover_matrix_rows",
                               return_value=self._ROWS):
            repro._resolve_pattern("ubu24-x86-gcc14", args)
        self.assertEqual(args.workflow, "explicit.yml")

    def test_glob_matching_multiple_rows_lists_and_exits(self):
        args = self._ns()
        with mock.patch.object(repro, "discover_matrix_rows",
                               return_value=self._ROWS):
            buf = io.StringIO()
            with redirect_stderr(buf), \
                 self.assertRaises(SystemExit) as cm:
                repro._resolve_pattern("ubu24-*", args)
        self.assertEqual(cm.exception.code, 1)
        # Both ubu24 rows surfaced.
        self.assertIn("ubu24-x86-gcc14", buf.getvalue())
        self.assertIn("ubu24-x86-clang22-asan", buf.getvalue())

    def test_no_match_lists_available_rows(self):
        """Common typo case: shows the user what's actually there
        instead of a bare 'no match' error."""
        args = self._ns()
        with mock.patch.object(repro, "discover_matrix_rows",
                               return_value=self._ROWS):
            with self.assertRaises(SystemExit) as cm:
                repro._resolve_pattern("nonexistent", args)
        msg = str(cm.exception)
        self.assertIn("no matrix row matches pattern 'nonexistent'", msg)
        self.assertIn("Available rows:", msg)
        self.assertIn("ubu24-x86-gcc14", msg)


# ---------------------------------------------------------------------------
# --ci-workflows local-action overlay
# ---------------------------------------------------------------------------

class LocalizeCiWorkflowsTests(unittest.TestCase):
    """`_localize_workflow_for_ci_workflows` is the file-rewriter
    behind `--ci-workflows <path>`. Verify the staged action layout,
    the `uses:` rewrites, and that lib/ + non-action dirs are skipped.
    Filesystem-heavy; uses tempdirs and chdir."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.consumer = self.tmp_path / "consumer"
        self.ci = self.tmp_path / "ci-workflows"
        (self.consumer / ".github" / "workflows").mkdir(parents=True)
        # Three action dirs in ci-workflows/actions/: two real, one
        # `lib` (non-action), one missing action.yml (skip).
        (self.ci / "actions" / "setup-recipe").mkdir(parents=True)
        (self.ci / "actions" / "setup-recipe" / "action.yml").write_text(
            "name: 'Setup recipe'\n")
        (self.ci / "actions" / "publish-recipe").mkdir(parents=True)
        (self.ci / "actions" / "publish-recipe" / "action.yml").write_text(
            "name: 'Publish recipe'\n")
        (self.ci / "actions" / "lib").mkdir(parents=True)
        (self.ci / "actions" / "lib" / "cache_io.py").write_text("# noop")
        (self.ci / "actions" / "no-action-yml-here").mkdir(parents=True)
        # The downstream workflow with a uses: that needs rewriting.
        self.wf = self.consumer / ".github" / "workflows" / "main.yml"
        self.wf.write_text(
            "jobs:\n  build:\n    steps:\n"
            "    - uses: compiler-research/ci-workflows/actions/setup-recipe@main\n"
            "      with:\n        recipe: llvm-asan\n"
            "    - uses: compiler-research/ci-workflows/actions/publish-recipe@v1\n")
        self._cwd = os.getcwd()
        os.chdir(self.consumer)

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def test_stages_real_actions_skips_lib_and_action_yml_less_dirs(self):
        repro._localize_workflow_for_ci_workflows(self.wf, self.ci)
        stage = self.consumer / ".github" / "act-ci-workflows-stage"
        self.assertTrue((stage / "setup-recipe" / "action.yml").is_file())
        self.assertTrue((stage / "publish-recipe" / "action.yml").is_file())
        # lib/ must be skipped (it's a Python module dir, not an action)
        self.assertFalse((stage / "lib").exists())
        # Dirs without action.yml are skipped too.
        self.assertFalse((stage / "no-action-yml-here").exists())

    def test_uses_rewritten_to_local_form(self):
        out = repro._localize_workflow_for_ci_workflows(self.wf, self.ci)
        text = Path(out).read_text()
        self.assertIn(
            "uses: ./.github/act-ci-workflows-stage/setup-recipe", text)
        self.assertIn(
            "uses: ./.github/act-ci-workflows-stage/publish-recipe", text)
        # Both at-refs gone.
        self.assertNotIn("@main", text)
        self.assertNotIn("@v1", text)

    def test_returns_temp_workflow_beside_original(self):
        out = repro._localize_workflow_for_ci_workflows(self.wf, self.ci)
        self.assertEqual(Path(out).parent, self.wf.parent)
        self.assertTrue(Path(out).name.startswith("act-main-localized-"))
        self.assertTrue(Path(out).name.endswith(".yml"))

    def test_stage_overwritten_on_re_run(self):
        """A leftover stage from a prior run shouldn't shadow the
        actions you're testing now."""
        repro._localize_workflow_for_ci_workflows(self.wf, self.ci)
        # Mutate the local action; re-localize.
        (self.ci / "actions" / "setup-recipe" / "action.yml").write_text(
            "name: 'Setup recipe (v2)'\n")
        repro._localize_workflow_for_ci_workflows(self.wf, self.ci)
        staged = (self.consumer / ".github" / "act-ci-workflows-stage"
                  / "setup-recipe" / "action.yml").read_text()
        self.assertIn("v2", staged)


class CleanupCiWorkflowsOverlayTests(unittest.TestCase):
    """Verify `_cleanup_ci_workflows_overlay` removes both the stage
    dir and any leftover act-*-localized-*.yml temp workflows."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.consumer = Path(self.tmp.name) / "consumer"
        (self.consumer / ".github" / "workflows").mkdir(parents=True)
        (self.consumer / ".github" / "act-ci-workflows-stage").mkdir()
        (self.consumer / ".github" / "act-ci-workflows-stage" / "marker"
         ).write_text("")
        (self.consumer / ".github" / "workflows"
         / "act-main-localized-abcd.yml").write_text("")
        # Untouched workflow that must NOT be removed.
        (self.consumer / ".github" / "workflows" / "main.yml").write_text("")
        self._cwd = os.getcwd()
        os.chdir(self.consumer)

    def tearDown(self):
        os.chdir(self._cwd)
        self.tmp.cleanup()

    def test_removes_stage_and_temp_workflow_keeps_originals(self):
        repro._cleanup_ci_workflows_overlay()
        stage = self.consumer / ".github" / "act-ci-workflows-stage"
        self.assertFalse(stage.exists())
        wf = self.consumer / ".github" / "workflows"
        self.assertFalse((wf / "act-main-localized-abcd.yml").exists())
        self.assertTrue((wf / "main.yml").exists())

    def test_idempotent_on_already_clean_workspace(self):
        repro._cleanup_ci_workflows_overlay()
        # Second call: stage gone, no temp workflow left. Should not raise.
        repro._cleanup_ci_workflows_overlay()


# ---------------------------------------------------------------------------
# End-to-end subprocess (--help, no act on PATH, etc.)
# ---------------------------------------------------------------------------

class SubprocessTests(unittest.TestCase):
    def _run(self, *args, **kw):
        cmd = [sys.executable, str(REPRO_PATH), *args]
        return subprocess.run(cmd, capture_output=True, text=True, **kw)

    def test_help_lists_essentials(self):
        r = self._run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--list", r.stdout)
        self.assertIn("--no-shell", r.stdout)
        self.assertIn("act", r.stdout)
        self.assertIn("Examples", r.stdout)

    def test_no_args_errors_with_useful_message(self):
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("-j NAME, --list, or -m name:", r.stderr)


if __name__ == "__main__":
    unittest.main()
