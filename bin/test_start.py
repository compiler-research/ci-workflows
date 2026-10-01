"""Unit tests for bin/start (the newcomer project picker).

Pins the parts that decide what a student ends up in: which entries
projects.yaml yields, which cell each names, how a project's own pin
overrides a stale catalog row, and what the menu says about a cell that
cannot be used.

The provisioning itself is bin/repro's and is tested in test_repro.py;
what is checked here is that start hands it a well-formed Namespace.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

START_PATH = Path(__file__).resolve().parent / "start"


def _load_start():
    loader = importlib.machinery.SourceFileLoader("start", str(START_PATH))
    spec = importlib.util.spec_from_loader("start", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


start = _load_start()


def _write(text: str) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "projects.yaml"
    tmp.write_text(text, encoding="utf-8")
    return tmp


WELL_FORMED = """\
# leading comment
projects:
  - name: CARTopiaX
    description: CAR T-cell model, built on BioDynaMo
    repo: https://github.com/compiler-research/CARTopiaX
    workflow: .github/workflows/ci.yml
    row: ubu24-gcc
    recipe: biodynamo
    version: v1.05-cr-20260812-01
    os: ubuntu-24.04
    arch: x86_64

  - name: CppInterOp
    description: C++ interop layer
    repo: https://github.com/compiler-research/CppInterOp
    workflow: .github/workflows/main.yml
    row: ubu24-x86-gcc12-llvm22
    recipe: llvm-release
    version: '22'
    os: ubuntu-24.04
    arch: x86_64
"""


class LoadProjectsTest(unittest.TestCase):
    def test_parses_every_entry_and_field(self):
        got = start.load_projects(_write(WELL_FORMED))
        self.assertEqual([p["name"] for p in got],
                         ["CARTopiaX", "CppInterOp"])
        self.assertEqual(got[0]["row"], "ubu24-gcc")
        # Quotes are stripped so '22' compares equal to the cells.yaml
        # value and to what compute_key.py is handed.
        self.assertEqual(got[1]["version"], "22")

    def test_description_may_contain_commas_and_colons(self):
        text = WELL_FORMED.replace(
            "description: C++ interop layer",
            "description: C++ interop: bindings, cppyy, and friends")
        got = start.load_projects(_write(text))
        self.assertEqual(got[1]["description"],
                         "C++ interop: bindings, cppyy, and friends")

    def test_entry_missing_a_required_field_is_dropped_not_raised(self):
        # A half-written row must cost the student that project only --
        # the rest of the menu still has to render.
        text = WELL_FORMED.replace("    arch: x86_64\n\n", "\n", 1)
        got = start.load_projects(_write(text))
        self.assertEqual([p["name"] for p in got], ["CppInterOp"])

    def test_missing_file_yields_empty_list(self):
        self.assertEqual(
            start.load_projects(Path(tempfile.mkdtemp()) / "nope.yaml"), [])

    def test_trailing_top_level_key_ends_the_block(self):
        got = start.load_projects(_write(WELL_FORMED + "\nother: value\n"))
        self.assertEqual(len(got), 2)

    def test_shipped_catalog_parses_and_every_coord_is_complete(self):
        for p in start.load_projects():
            coord = start.coord_of(p)
            self.assertTrue(all(coord.values()), p)


class CoordTest(unittest.TestCase):
    def test_coord_str_is_the_direct_coord_repro_accepts(self):
        p = start.load_projects(_write(WELL_FORMED))[0]
        self.assertEqual(
            start.coord_str(start.coord_of(p)),
            "biodynamo/v1.05-cr-20260812-01/ubuntu-24.04/x86_64")


_STEP = """\
    steps:
      - uses: compiler-research/ci-workflows/actions/setup-llvm@main
        with:
          version: ${{ matrix.clang-runtime }}
          os: ${{ matrix.os }}
          flavor: ${{ matrix.flavor }}
"""


class RepoCellTest(unittest.TestCase):
    """The project, once cloned, outranks the catalog -- read through
    cells.scan, so every coord field counts, not just the version."""

    ROW = "ubu24-x86-gcc12-llvm22"

    def _checkout(self, include: str) -> Path:
        root = Path(tempfile.mkdtemp())
        wf = root / ".github" / "workflows" / "main.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text("jobs:\n  build:\n    runs-on: ${{ matrix.os }}\n"
                      "    strategy:\n      matrix:\n        include:\n"
                      + include + _STEP, encoding="utf-8")
        return root

    def _project(self, **over):
        p = {"workflow": ".github/workflows/main.yml", "row": self.ROW}
        p.update(over)
        return p

    def _cell(self, root, **over):
        c = start.repo_cell(root, self._project(**over))
        return start.coord_str(c) if c else None

    def test_block_row(self):
        root = self._checkout(
            "          - name: ubu24-x86-gcc12-llvm22\n"
            "            os: ubuntu-24.04\n"
            "            clang-runtime: '23'\n")
        self.assertEqual(self._cell(root), "llvm-release/23/ubuntu-24.04/x86_64")

    def test_inline_row(self):
        root = self._checkout(
            "          - { name: ubu24-x86-gcc12-llvm22, os: ubuntu-24.04, "
            "clang-runtime: '22' }\n")
        self.assertEqual(self._cell(root), "llvm-release/22/ubuntu-24.04/x86_64")

    def test_a_changed_recipe_is_seen_not_just_a_version(self):
        root = self._checkout(
            "          - { name: ubu24-x86-gcc12-llvm22, os: ubuntu-24.04, "
            "clang-runtime: '22', flavor: debug }\n")
        self.assertEqual(self._cell(root), "llvm-debug/22/ubuntu-24.04/x86_64")

    def test_a_longer_row_name_is_not_mistaken_for_this_row(self):
        # Both are real CppInterOp rows; a substring match once read the
        # -cppyy row's LLVM 21 for the plain row.
        root = self._checkout(
            "          - { name: ubu24-x86-gcc12-llvm22-cppyy, os: ubuntu-24.04, "
            "clang-runtime: '21' }\n"
            "          - { name: ubu24-x86-gcc12-llvm22, os: ubuntu-24.04, "
            "clang-runtime: '22' }\n")
        self.assertEqual(self._cell(root), "llvm-release/22/ubuntu-24.04/x86_64")

    def test_other_row_or_other_workflow_is_not_picked_up(self):
        root = self._checkout(
            "          - { name: other-row, os: ubuntu-24.04, "
            "clang-runtime: '22' }\n")
        self.assertIsNone(self._cell(root))
        root = self._checkout(
            "          - { name: ubu24-x86-gcc12-llvm22, os: ubuntu-24.04, "
            "clang-runtime: '22' }\n")
        self.assertIsNone(self._cell(root, workflow=".github/workflows/x.yml"))

    def test_missing_workflow_is_silent(self):
        self.assertIsNone(start.repo_cell(Path(tempfile.mkdtemp()),
                                          self._project()))


class RenderTest(unittest.TestCase):
    def test_each_project_shows_its_cell_and_status(self):
        projects = start.load_projects(_write(WELL_FORMED))
        with redirect_stdout(io.StringIO()) as out:
            start.render(projects, ["published -- 181 MB download",
                                    "not published yet"])
        text = out.getvalue()
        self.assertIn("1  CARTopiaX", text)
        self.assertIn("2  CppInterOp", text)
        self.assertIn("biodynamo v1.05-cr-20260812-01", text)
        self.assertIn("181 MB", text)
        self.assertIn("not published yet", text)


class DetectProjectTest(unittest.TestCase):
    """Running from inside a checkout must not ask which project it is."""

    def test_slug_normalises_ssh_https_and_dot_git(self):
        for url in ("https://github.com/compiler-research/CARTopiaX",
                    "https://github.com/compiler-research/CARTopiaX.git",
                    "git@github.com:compiler-research/CARTopiaX.git",
                    "https://github.com/compiler-research/CARTopiaX/"):
            self.assertEqual(start._repo_slug(url),
                             "compiler-research/cartopiax", url)

    def test_matches_on_the_origin_remote_not_the_directory_name(self):
        projects = start.load_projects(_write(WELL_FORMED))
        repro = mock.Mock()
        repro._origin_repo_slug.return_value = "compiler-research/CARTopiaX"
        with mock.patch.object(start.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0,
                                         stdout="/home/s/renamed-dir\n")
            got = start.detect_project(repro, projects)
        self.assertIsNotNone(got)
        self.assertEqual(got[0]["name"], "CARTopiaX")
        self.assertEqual(got[1], Path("/home/s/renamed-dir"))

    def test_uncatalogued_or_non_git_cwd_falls_through_to_the_menu(self):
        projects = start.load_projects(_write(WELL_FORMED))
        repro = mock.Mock()
        repro._origin_repo_slug.return_value = "someone/unrelated"
        self.assertIsNone(start.detect_project(repro, projects))
        repro._origin_repo_slug.return_value = None
        self.assertIsNone(start.detect_project(repro, projects))


class CellStatusTest(unittest.TestCase):
    """Three outcomes, three different things for the student to do."""

    PROJECT = {"recipe": "biodynamo", "version": "v1", "os": "ubuntu-24.04",
               "arch": "x86_64"}
    CELLS = [{"recipe": "biodynamo", "version": "v1", "os": "ubuntu-24.04",
              "arch": "x86_64"}]

    def test_coord_absent_from_cells_yaml_is_called_stale(self):
        repro = mock.Mock()
        msg = start.cell_status(repro, "https://example/", self.PROJECT, [])
        self.assertIn("cells.yaml", msg)
        repro._devshell_compute_key.assert_not_called()

    def test_known_cell_with_no_asset_reads_as_not_published(self):
        repro = mock.Mock()
        repro._devshell_compute_key.return_value = "k"
        with mock.patch.object(start, "_asset_size", return_value=None):
            msg = start.cell_status(repro, "https://example/",
                                    self.PROJECT, self.CELLS)
        self.assertIn("not published", msg)

    def test_published_cell_reports_download_size_in_mib(self):
        repro = mock.Mock()
        repro._devshell_compute_key.return_value = "k"
        with mock.patch.object(start, "_asset_size",
                               return_value=181 * 1024 * 1024):
            msg = start.cell_status(repro, "https://example/",
                                    self.PROJECT, self.CELLS)
        self.assertIn("181 MB", msg)


class SelectTest(unittest.TestCase):
    def test_accepts_a_valid_number_as_zero_based_index(self):
        with mock.patch("builtins.input", side_effect=["2"]):
            self.assertEqual(start.select(3), 1)

    def test_reprompts_on_garbage_then_accepts(self):
        with mock.patch("builtins.input", side_effect=["x", "9", "1"]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(start.select(3), 0)

    def test_quit_and_eof_both_mean_no_selection(self):
        with mock.patch("builtins.input", side_effect=["q"]):
            self.assertIsNone(start.select(3))
        # EOF matters on its own: a piped run must exit rather than
        # spin on an endless stream of empty reads.
        with mock.patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(io.StringIO()):
                self.assertIsNone(start.select(3))


class LaunchTest(unittest.TestCase):
    """bin/start reaches bin/repro only through its command line."""

    def _launch(self, coord, checkout="/somewhere/CARTopiaX"):
        repro = start._load_repro()
        with mock.patch.object(repro, "cmd_devshell",
                               return_value=0) as cmd, \
                redirect_stdout(io.StringIO()) as out:
            rc = start.launch(repro, {}, Path(checkout), coord)
        return rc, cmd.call_args[0][0], out.getvalue()

    def test_argv_goes_through_repro_main_and_parser(self):
        project = start.load_projects(_write(WELL_FORMED))[0]
        rc, ns, out = self._launch(start.coord_of(project))
        self.assertEqual(rc, 0)
        # Whatever repro's parser produces is what cmd_devshell reads,
        # so a flag added to repro can't be missing here.
        self.assertEqual(
            ns.matrix,
            ["name:biodynamo/v1.05-cr-20260812-01/ubuntu-24.04/x86_64"])
        # Host-cache mode is the point for a newcomer: the download has
        # to outlive the container.
        self.assertTrue(ns.devshell_host_cache)
        self.assertEqual(ns.devshell_patches_out,
                         str(Path("/somewhere/CARTopiaX")))
        # The same command is printed, for reopening the shell later.
        self.assertIn(" ".join(start.devshell_argv(
            Path("/somewhere/CARTopiaX"), start.coord_of(project))), out)

    def test_argv_is_accepted_by_the_real_parser(self):
        repro = start._load_repro()
        coord = {"recipe": "llvm-release", "version": "22",
                 "os": "ubuntu-24.04", "arch": "x86_64"}
        ns = repro.parse_args(start.devshell_argv(Path("/x"), coord))
        self.assertTrue(ns.devshell)
        self.assertEqual(ns.passthrough,
                         ["llvm-release/22/ubuntu-24.04/x86_64"])


class ResolveCheckoutTest(unittest.TestCase):
    """The one function that creates directories and runs git clone."""

    PROJECT = {"name": "CARTopiaX", "recipe": "biodynamo", "version": "v1",
               "repo": "https://github.com/compiler-research/CARTopiaX"}

    def test_existing_checkout_is_reused_and_never_touched(self):
        # .resolve(), because resolve_checkout does: on macOS /var is a
        # symlink to /private/var and the two spellings differ.
        dest = (Path(tempfile.mkdtemp()) / "CARTopiaX").resolve()
        (dest / ".git").mkdir(parents=True)
        with mock.patch("builtins.input", side_effect=[str(dest)]), \
                mock.patch.object(start.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            got = start.resolve_checkout(self.PROJECT)
        self.assertEqual(got, dest)
        # No pull, no reset, no clone: it is the user's working copy.
        run.assert_not_called()

    def test_existing_non_git_directory_is_refused(self):
        # Cloning into it would fail and mkdir-ing over it would be
        # worse; refusing is the only safe answer.
        dest = Path(tempfile.mkdtemp()) / "notarepo"
        dest.mkdir()
        with mock.patch("builtins.input", side_effect=[str(dest)]), \
                mock.patch.object(start.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(start.resolve_checkout(self.PROJECT))
        run.assert_not_called()

    def test_declining_the_clone_prompt_creates_nothing(self):
        dest = Path(tempfile.mkdtemp()) / "nope" / "CARTopiaX"
        with mock.patch("builtins.input", side_effect=[str(dest), "n"]), \
                mock.patch.object(start.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(start.resolve_checkout(self.PROJECT))
        run.assert_not_called()
        self.assertFalse(dest.parent.exists())

    def test_clone_failure_reports_and_yields_no_path(self):
        dest = Path(tempfile.mkdtemp()) / "CARTopiaX"
        with mock.patch("builtins.input", side_effect=[str(dest), ""]), \
                mock.patch.object(start.subprocess, "run") as run, \
                redirect_stdout(io.StringIO()):
            run.return_value = mock.Mock(returncode=128)
            self.assertIsNone(start.resolve_checkout(self.PROJECT))

    def test_default_is_the_current_directory(self):
        here = Path(tempfile.mkdtemp()).resolve()
        with mock.patch("builtins.input", side_effect=["", "n"]), \
                mock.patch.object(start.Path, "cwd", return_value=here), \
                redirect_stdout(io.StringIO()) as out:
            start.resolve_checkout(self.PROJECT)
        self.assertIn(f"clone to    {here / 'CARTopiaX'}", out.getvalue())

    def test_default_inside_a_checkout_is_beside_it(self):
        # ci-workflows itself is the case the README sends people into;
        # a dotfiles repository at $HOME must not send clones to /Users.
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "ci-workflows" / ".git").mkdir(parents=True)
        (root / "ci-workflows" / "bin").mkdir()
        self.assertEqual(start.default_clone_parent(root / "ci-workflows" / "bin"), root)
        (root / ".git").mkdir()
        with mock.patch.object(start.Path, "home", return_value=root):
            self.assertEqual(start.default_clone_parent(root), root)

    def test_eof_at_the_path_prompt_is_not_a_traceback(self):
        with mock.patch("builtins.input", side_effect=EOFError), \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(start.resolve_checkout(self.PROJECT))


class PreflightTest(unittest.TestCase):
    """Preflight gates everything; always-true defeats it, always-false
    bricks the tool."""

    def test_a_blocking_check_fails_preflight(self):
        with mock.patch.object(start, "_docker_status",
                               return_value=(False, "not installed")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertFalse(start.preflight())
        self.assertIn("!", out.getvalue())

    def test_all_clear_passes(self):
        with mock.patch.object(start, "_docker_status",
                               return_value=(True, "29.4.0, daemon up")), \
                redirect_stdout(io.StringIO()):
            self.assertTrue(start.preflight())

    def test_low_disk_reports_but_does_not_block(self):
        # Advisory by design: preflight runs before a project is
        # picked, and the cells differ by an order of magnitude.
        with mock.patch.object(start.shutil, "disk_usage") as du:
            du.return_value = mock.Mock(free=2 * 1024 ** 3)
            ok, detail = start._disk_status()
        self.assertTrue(ok)
        self.assertIn("tight", detail)


class ListModeTest(unittest.TestCase):
    def test_list_prints_the_catalog_and_exits_without_prompting(self):
        with mock.patch.object(start, "_load_repro") as load, \
                mock.patch.object(start, "cell_status",
                                  return_value="published -- 1 MB download"), \
                mock.patch("builtins.input",
                           side_effect=AssertionError("must not prompt")), \
                redirect_stdout(io.StringIO()) as out:
            load.return_value.published_cells.return_value = []
            self.assertEqual(start.main(["--list"]), 0)
        self.assertIn("CARTopiaX", out.getvalue())


class AssetSizeTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32",
                     "file:// paths are POSIX-shaped here, as in cache_io")
    def test_file_backend_reports_size_and_absence(self):
        d = Path(tempfile.mkdtemp())
        (d / "k.tar.zst").write_bytes(b"x" * 1234)
        self.assertEqual(start._asset_size(f"file://{d}", "k"), 1234)
        self.assertIsNone(start._asset_size(f"file://{d}", "absent"))

    def test_unsupported_scheme_is_none_not_an_exception(self):
        self.assertIsNone(start._asset_size("s3://bucket", "k"))


if __name__ == "__main__":
    unittest.main()


_CLAD_LIKE_CI = """\
on: push
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - { name: sys18, os: ubuntu-24.04-arm, clang-runtime: '18' }
          - { name: rel22, os: ubuntu-24.04, clang-runtime: '22', use-recipe: 'true' }
          - { name: asan23, os: ubuntu-24.04, clang-runtime: '23', flavor: asan }
          - { name: mac23, os: macos-26, clang-runtime: '23', use-recipe: 'true' }
    steps:
      - uses: compiler-research/ci-workflows/actions/setup-llvm@main
        with:
          version: ${{ matrix.clang-runtime }}
          os:      ${{ matrix.os }}
          flavor:  ${{ matrix.use-recipe != 'true' && (matrix.flavor || 'system') || '' }}
"""

_CELLS = [
    {"recipe": "llvm-release", "version": "22", "os": "ubuntu-24.04",
     "arch": "x86_64"},
    {"recipe": "llvm-asan", "version": "23", "os": "ubuntu-24.04",
     "arch": "x86_64"},
    {"recipe": "llvm-release", "version": "23", "os": "macos-26",
     "arch": "arm64"},
]


def _checkout(ci: Optional[str] = _CLAD_LIKE_CI) -> Path:
    root = Path(tempfile.mkdtemp()).resolve() / "clad"
    (root / ".git").mkdir(parents=True)
    if ci is not None:
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(ci, encoding="utf-8")
    return root


def _fake_repro():
    """The real repro, minus the network and the container."""
    repro = start._load_repro()
    repro._devshell_compute_key = mock.Mock(return_value="k")
    repro.cmd_devshell = mock.Mock(return_value=0)
    return repro


class AnyRepositoryTest(unittest.TestCase):
    """bin/start on a repository outside projects.yaml, clad as the
    example: the cell comes from the repo's own workflows."""

    def test_plainest_servable_cell_is_the_default(self):
        with mock.patch("builtins.input", side_effect=[""]), \
                mock.patch.object(start, "_asset_size", return_value=None), \
                redirect_stdout(io.StringIO()) as out:
            got = start.start_checkout(_fake_repro(), "file:///x", _CELLS,
                                       _checkout())
        project, coord = got
        self.assertEqual(coord, _CELLS[0])  # llvm-release before asan
        self.assertEqual(project["name"], "clad")
        text = out.getvalue()
        # macOS is in cells.yaml but has no dev container; say so.
        self.assertIn("llvm-release/23/macos-26/arm64", text)
        self.assertIn("no dev container", text)

    def test_quit_at_the_toolchain_prompt(self):
        with mock.patch("builtins.input", side_effect=["q"]), \
                mock.patch.object(start, "_asset_size", return_value=None), \
                redirect_stdout(io.StringIO()):
            got = start.start_checkout(_fake_repro(), "file:///x", _CELLS,
                                       _checkout())
        self.assertIs(got, start.QUIT)

    def test_repository_not_using_ci_workflows_is_refused(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(start.start_checkout(
                _fake_repro(), "file:///x", _CELLS, _checkout(ci=None)))
        self.assertIn("does not use compiler-research/ci-workflows",
                      out.getvalue())

    def test_only_system_rows_means_nothing_to_offer(self):
        ci = _CLAD_LIKE_CI.replace("use-recipe: 'true'", "x: y").replace(
            "flavor: asan", "x: z")
        with redirect_stdout(io.StringIO()) as out, \
                mock.patch("builtins.input",
                           side_effect=AssertionError("must not prompt")):
            self.assertIsNone(start.start_checkout(
                _fake_repro(), "file:///x", _CELLS, _checkout(ci)))
        self.assertIn("flavor=system", out.getvalue())

    def test_owner_repo_shorthand_clones_from_github(self):
        with mock.patch.object(start, "resolve_checkout",
                               return_value=None) as rc:
            start.custom_checkout("vgvassilev/clad")
        self.assertEqual(rc.call_args[0][0],
                         {"name": "clad",
                          "repo": "https://github.com/vgvassilev/clad"})

    def test_existing_path_is_used_without_cloning(self):
        co = _checkout()
        with mock.patch.object(start, "resolve_checkout",
                               side_effect=AssertionError("no clone")):
            self.assertEqual(start.custom_checkout(str(co / ".github")), co)

    def test_main_in_an_uncatalogued_checkout_skips_the_menu(self):
        co = _checkout()
        repro = _fake_repro()
        repro._origin_repo_slug = mock.Mock(return_value="someone/uncatalogued")
        repro.published_cells = mock.Mock(return_value=_CELLS)
        with mock.patch.object(start, "preflight", return_value=True), \
                mock.patch.object(start, "_load_repro", return_value=repro), \
                mock.patch.object(start, "_asset_size", return_value=None), \
                mock.patch.object(start.Path, "cwd", return_value=co), \
                mock.patch("builtins.input", side_effect=["2"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(start.main([]), 0)
        self.assertNotIn("select a project", out.getvalue())
        ns = repro.cmd_devshell.call_args[0][0]
        self.assertEqual(ns.matrix,
                         ["name:llvm-asan/23/ubuntu-24.04/x86_64"])
        self.assertEqual(ns.devshell_patches_out, str(co))

    def test_clad_is_in_the_catalog(self):
        # The real projects.yaml: clad is a menu entry, and its row is
        # the one a clad-shaped workflow resolves to a published cell.
        clad = [p for p in start.load_projects() if p["name"] == "clad"]
        self.assertEqual(len(clad), 1)
        self.assertEqual(clad[0]["repo"], "https://github.com/vgvassilev/clad")
        cell = start.coord_of(clad[0])
        self.assertTrue(start.cells.in_catalog(
            cell, start.cells.load_catalog()))
