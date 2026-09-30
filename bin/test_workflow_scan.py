"""Unit tests for bin/workflow_scan.py.

Pins the three layers that decide which cell a consumer's row maps to:
the YAML subset, the expression evaluator, and matrix expansion -- and
then the end-to-end scan over workflows shaped like clad's, CppInterOp's
and CARTopiaX's, whose conventions for reaching a cell all differ.
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_scan as ws  # noqa: E402


def _y(text: str):
    return ws.parse_yaml(textwrap.dedent(text))


class YAMLTest(unittest.TestCase):
    def test_block_and_flow_collections(self):
        doc = _y("""
            jobs:
              build:
                strategy:
                  matrix:
                    include:
                      - { name: a, os: ubuntu-24.04, clang-runtime: '23' }
                      - name: b
                        os: [x, 'y']
        """)
        inc = doc["jobs"]["build"]["strategy"]["matrix"]["include"]
        self.assertEqual(inc[0], {"name": "a", "os": "ubuntu-24.04",
                                  "clang-runtime": "23"})
        self.assertEqual(inc[1], {"name": "b", "os": ["x", "y"]})

    def test_scalars_are_typed_like_yaml_1_2(self):
        doc = _y("""
            a: 22
            b: '22'
            c: true
            d: on
            e: ~
            f: 3.10
        """)
        # `on` stays a string: GitHub's `on:` key depends on it.
        self.assertEqual(doc, {"a": 22, "b": "22", "c": True, "d": "on",
                               "e": None, "f": 3.1})

    def test_anchors_aliases_and_merge_keys(self):
        doc = _y("""
            base: &b { os: ubuntu-24.04, v: &ver '22' }
            steps: &s
              - run: echo
            again: *s
            row:
              <<: *b
              name: r
            list: [*ver]
        """)
        self.assertEqual(doc["again"], [{"run": "echo"}])
        self.assertEqual(doc["row"], {"os": "ubuntu-24.04", "v": "22",
                                      "name": "r"})
        self.assertEqual(doc["list"], ["22"])

    def test_block_scalars_and_comments(self):
        doc = _y("""
            run: |
              echo "a # not a comment"
              echo b
            if: >-
              matrix.x &&
              matrix.y   # trailing
            note: don't # a comment
        """)
        self.assertEqual(doc["run"], 'echo "a # not a comment"\necho b\n')
        self.assertEqual(doc["if"], "matrix.x && matrix.y   # trailing")
        self.assertEqual(doc["note"], "don't")


class ExprTest(unittest.TestCase):
    def test_clad_flavor_expression(self):
        expr = ("matrix.use-recipe != 'true' && "
                "(matrix.flavor || 'system') || ''")
        ev = lambda m: ws.evaluate(expr, {"matrix": m})  # noqa: E731
        self.assertEqual(ev({}), "system")
        self.assertEqual(ev({"use-recipe": "true"}), "")
        self.assertEqual(ev({"flavor": "asan"}), "asan")

    def test_functions_and_loose_equality(self):
        ctx = {"matrix": {"os": "Self-Hosted-x", "n": 22}}
        self.assertTrue(ws.evaluate("contains(matrix.os, 'self-hosted')",
                                    ctx))
        self.assertEqual(ws.evaluate("format('{0}-llvm{1}', 'cling', "
                                     "matrix.n)", ctx), "cling-llvm22")
        self.assertTrue(ws.evaluate("matrix.n == '22'", ctx))
        self.assertTrue(ws.evaluate("MATRIX.OS == 'self-hosted-X'", ctx))

    def test_render_keeps_type_of_a_lone_expression(self):
        ctx = {"matrix": {"v": 22}}
        self.assertEqual(ws.render("${{ matrix.v }}", ctx), 22)
        self.assertEqual(ws.render("ROOT-llvm${{ matrix.v }}", ctx),
                         "ROOT-llvm22")

    def test_step_if_reading_runtime_state_is_assumed_true(self):
        ctx = {"matrix": {}, "runner": {"os": "Linux"}}
        self.assertFalse(ws.step_runs("${{ matrix.cuda }}", ctx))
        self.assertFalse(ws.step_runs("runner.os == 'Windows'", ctx))
        self.assertTrue(ws.step_runs("github.event_name == 'nope'", ctx))
        self.assertTrue(ws.step_runs(None, ctx))


class MatrixTest(unittest.TestCase):
    def test_include_only_rows_stay_separate(self):
        # Regression: a later include must not merge into a row an
        # earlier include added -- that collapsed clad's 30 rows to 1.
        rows = ws.expand_matrix({"include": [{"name": "a", "x": 1},
                                             {"name": "b", "y": 2}]})
        self.assertEqual(rows, [{"name": "a", "x": 1},
                                {"name": "b", "y": 2}])

    def test_product_exclude_and_merging_include(self):
        rows = ws.expand_matrix({
            "os": ["u", "m"], "v": [1, 2],
            "exclude": [{"os": "m", "v": 1}],
            "include": [{"os": "u", "extra": "e"}, {"os": "w", "v": 3}],
        })
        self.assertEqual(rows, [
            {"os": "u", "v": 1, "extra": "e"},
            {"os": "u", "v": 2, "extra": "e"},
            {"os": "m", "v": 2},
            {"os": "w", "v": 3},
        ])

    def test_dynamic_matrix_is_unknown(self):
        self.assertIsNone(ws.expand_matrix("${{ fromJSON(needs.a.x) }}"))
        self.assertEqual(ws.expand_matrix(None), [{}])


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
    return {e["row"]: "/".join(e[k] for k in ws.COORD_KEYS)
            for e in ws.scan_repo(repo)}


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

    def test_refs_detect_use_of_ci_workflows(self):
        self.assertTrue(ws.ci_workflows_refs(
            _repo({".github/workflows/ci.yml": CLAD_LIKE})))
        self.assertEqual(ws.ci_workflows_refs(_repo({
            ".github/workflows/ci.yml":
                "jobs: {a: {steps: [{uses: actions/checkout@v4}]}}\n"})), [])
        self.assertEqual(ws.ci_workflows_refs(Path(tempfile.mkdtemp())), [])


if __name__ == "__main__":
    unittest.main()
