"""Unit tests for bin/cells.py: resolving a consumer's CI rows to
recipe cells, over workflows shaped like clad's, CppInterOp's and
CARTopiaX's -- whose conventions for reaching a cell all differ."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cells  # noqa: E402


def _repo(files):
    root = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


CLAD_LIKE = """
    on: push
    jobs:
      build:
        runs-on: ${{ matrix.os }}
        strategy:
          matrix:
            include:
              - { name: sys18, os: ubuntu-24.04-arm, clang-runtime: '18' }
              - { name: rel23, os: ubuntu-24.04, clang-runtime: '23', use-recipe: 'true' }
              - { name: mac23, os: macos-26, clang-runtime: '23', use-recipe: 'true' }
              - { name: dbg22, os: ubuntu-24.04, clang-runtime: '22', flavor: debug }
              - { name: win23, os: windows-2022, clang-runtime: '23' }
        steps: &steps
          - uses: actions/checkout@v4
          - name: Setup LLVM
            if: runner.os != 'Windows'
            uses: compiler-research/ci-workflows/actions/setup-llvm@main
            with:
              version: ${{ matrix.clang-runtime }}
              os:      ${{ matrix.self-hosted-os || matrix.os }}
              flavor:  ${{ matrix.use-recipe != 'true' && (matrix.flavor || 'system') || '' }}
      wasm:
        runs-on: ubuntu-24.04
        steps:
          - uses: compiler-research/ci-workflows/actions/setup-recipe@main
            with: { recipe: llvm-wasm, version: '22', os: ubuntu-24.04, arch: x86_64 }
"""

CPPINTEROP_LIKE = """
    on: push
    jobs:
      build:
        runs-on: ${{ matrix.os }}
        strategy:
          matrix:
            include:
              - name: plain
                os: ubuntu-24.04
                clang-runtime: '22'
              - name: cling
                os: ubuntu-24.04
                clang-runtime: '22'
                flavor: cling
                cling-patches: ROOT
        steps:
          - uses: compiler-research/ci-workflows/actions/setup-llvm@main
            with:
              version: ${{ matrix.clang-runtime }}
              os: ${{ matrix.os }}
              flavor: ${{ matrix.flavor }}
              flavor-version: ${{ matrix.cling-patches && format('{0}-llvm{1}', matrix.cling-patches, matrix.clang-runtime) || matrix.flavor-version }}
"""


def _cells(repo):
    """row -> its cells, as coord strings."""
    return {r.row: ", ".join(cells.coord_str(c) for _via, c in r.cells)
            for r in cells.scan(repo) if r.cells}


class ScanTest(unittest.TestCase):
    def test_clad_shaped_matrix(self):
        got = _cells(_repo({".github/workflows/ci.yml": CLAD_LIKE}))
        self.assertEqual(got, {
            # sys18: flavor=system (apt), no cell. win23: step skipped.
            "rel23": "llvm-release/23/ubuntu-24.04/x86_64",
            "mac23": "llvm-release/23/macos-26/arm64",
            "dbg22": "llvm-debug/22/ubuntu-24.04/x86_64",
            "wasm":  "llvm-wasm/22/ubuntu-24.04/x86_64",
        })

    def test_cppinterop_shaped_matrix(self):
        # Here an absent flavor is setup-llvm's default '' (llvm-release),
        # not clad's 'system' -- only evaluating the `with:` gets both.
        got = _cells(_repo({".github/workflows/main.yml": CPPINTEROP_LIKE}))
        self.assertEqual(got, {
            "plain": "llvm-release/22/ubuntu-24.04/x86_64",
            "cling": "llvm-root/ROOT-llvm22/ubuntu-24.04/x86_64",
        })

    def test_composite_of_ours_is_followed(self):
        # setup-biodynamo -> setup-recipe, with arch from a shell step
        # output we cannot see; falls back to the os-derived arch.
        repo = _repo({".github/workflows/ci.yml": """
            jobs:
              b:
                runs-on: ubuntu-24.04
                strategy:
                  matrix:
                    include: [{ name: g, os: ubuntu-24.04, recipe-version: v1 }]
                steps:
                  - uses: compiler-research/ci-workflows/actions/setup-biodynamo@main
                    with: { version: '${{ matrix.recipe-version }}', os: '${{ matrix.os }}' }
        """})
        self.assertEqual(_cells(repo),
                         {"g": "biodynamo/v1/ubuntu-24.04/x86_64"})

    def test_system_rows_are_kept_with_a_note(self):
        rows = cells.scan(_repo({".github/workflows/ci.yml": CLAD_LIKE}))
        sys18 = cells.find_row(rows, "sys18")
        self.assertIsNone(sys18.primary)
        self.assertIn("flavor=system", sys18.notes[0])


class ResolverTest(unittest.TestCase):
    def test_setup_llvm_table(self):
        r = cells.RESOLVERS["setup-llvm"]
        base = {"version": "22", "os": "ubuntu-24.04-arm", "arch": ""}
        self.assertEqual(r(dict(base, flavor=""))[0],
                         {"recipe": "llvm-release", "version": "22",
                          "os": "ubuntu-24.04-arm", "arch": "arm64"})
        self.assertEqual(r(dict(base, flavor="cling",
                                **{"flavor-version": "cling-llvm22"}))[0]
                         ["version"], "cling-llvm22")
        self.assertIsNone(r(dict(base, flavor="system"))[0])
        self.assertIn("unknown flavor", r(dict(base, flavor="zz"))[1])

    def test_flavor_table_matches_setup_llvm_action(self):
        # cells.FLAVOR_TO_RECIPE restates the `case` in setup-llvm's
        # "Resolve flavor -> recipe" step, the one mapping done in shell.
        # Parse the action so the two cannot drift apart unnoticed.
        import re
        text = (cells.REPO_ROOT / "actions" / "setup-llvm" /
                "action.yml").read_text()
        body = text[text.index('case "${FLAVOR}" in'):text.index("esac")]
        table = {}
        for m in re.finditer(r"^\s*('?)([a-z]*)\1\)\s*\n?\s*recipe=(\S*)",
                             body, re.M):
            table[m.group(2)] = m.group(3).strip("'") or None
        self.assertEqual(table, cells.FLAVOR_TO_RECIPE)

    def test_a_new_resolver_is_picked_up_by_scan(self):
        # The extension point: registering is all it takes.
        @cells.resolves("setup-foo")
        def _foo(inputs):
            return cells.make_coord("foo", inputs.get("v"), "ubuntu-24.04"), ""
        try:
            repo = _repo({".github/workflows/ci.yml": """
                jobs:
                  j:
                    runs-on: ubuntu-24.04
                    steps:
                      - uses: compiler-research/ci-workflows/actions/setup-foo@main
                        with: { v: '1' }
            """})
            self.assertEqual(_cells(repo), {"j": "foo/1/ubuntu-24.04/x86_64"})
        finally:
            del cells.RESOLVERS["setup-foo"]


class CatalogTest(unittest.TestCase):
    def test_real_cells_yaml(self):
        cat = cells.load_catalog()
        self.assertTrue(cat)
        self.assertTrue(cells.in_catalog(
            {"recipe": "llvm-release", "version": "22",
             "os": "ubuntu-24.04", "arch": "x86_64"}, cat))

    def test_missing_file_is_empty(self):
        self.assertEqual(
            cells.load_catalog(Path(tempfile.mkdtemp()) / "nope.yaml"), [])

    def test_plainest_first(self):
        mk = lambda r, v: {"recipe": r, "version": v, "os": "o", "arch": "a"}  # noqa: E731
        items = [(mk("llvm-asan", "23"), 1), (mk("llvm-release", "22"), 3),
                 (mk("llvm-release", "23"), 1), (mk("llvm-wasm", "22"), 5)]
        items.sort(key=lambda i: cells.plainest_first(*i))
        self.assertEqual([cells.coord_str(c) for c, _n in items], [
            "llvm-release/22/o/a", "llvm-release/23/o/a",
            "llvm-wasm/22/o/a", "llvm-asan/23/o/a"])


if __name__ == "__main__":
    unittest.main()
