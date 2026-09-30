"""Unit tests for bin/gha.py: the YAML subset, the expression evaluator,
matrix expansion, and walking steps into composites."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gha as ws  # noqa: E402


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


class WalkTest(unittest.TestCase):
    def test_rows_and_calls_through_a_local_composite(self):
        root = Path(tempfile.mkdtemp())
        files = {
            ".github/workflows/ci.yml": """
                jobs:
                  b:
                    runs-on: ${{ matrix.os }}
                    strategy:
                      matrix:
                        os: [ubuntu-24.04, windows-2022]
                    steps:
                      - uses: ./.github/actions/mine
                        with: { v: '22' }
            """,
            ".github/actions/mine/action.yml": """
                inputs:
                  v: { required: true }
                  flavor: { default: asan }
                runs:
                  using: composite
                  steps:
                    - if: runner.os == 'Linux'
                      uses: compiler-research/ci-workflows/actions/setup-llvm@main
                      with:
                        version: ${{ inputs.v }}
                        flavor: ${{ inputs.flavor }}
            """,
        }
        for rel, text in files.items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(textwrap.dedent(text), encoding="utf-8")
        got = []
        for row in ws.iter_rows(root):
            for call in ws.iter_calls(row.steps, row.ctx, root,
                                      {"setup-llvm"}):
                got.append((row.name, call.via, call.action,
                            call.inputs["version"], call.inputs["flavor"]))
        # windows row: the composite's step `if:` is false.
        self.assertEqual(got, [("b (os=ubuntu-24.04)",
                                "./.github/actions/mine", "setup-llvm",
                                "22", "asan")])

    def test_refs_detect_use_of_ci_workflows(self):
        root = Path(tempfile.mkdtemp())
        (root / ".github" / "workflows").mkdir(parents=True)
        wf = root / ".github" / "workflows" / "ci.yml"
        wf.write_text("jobs: {a: {steps: [{uses: actions/checkout@v4}]}}\n")
        self.assertEqual(ws.ci_workflows_refs(root), [])
        wf.write_text("jobs: {a: {uses: compiler-research/ci-workflows/"
                      ".github/workflows/x.yml@main}}\n")
        self.assertEqual(len(ws.ci_workflows_refs(root)), 1)
        self.assertEqual(ws.ci_workflows_refs(Path(tempfile.mkdtemp())), [])

    def test_workflow_paths_are_posix_on_every_os(self):
        # projects.yaml names workflows `/`-separated, and repo_cell
        # compares against it; str() of a relative path would give
        # `.github\\workflows\\ci.yml` on Windows and never match.
        root = Path(tempfile.mkdtemp())
        wf = root / ".github" / "workflows" / "ci.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text("jobs:\n  a:\n    steps:\n      - uses: "
                      "compiler-research/ci-workflows/actions/setup-llvm@main\n",
                      encoding="utf-8")
        self.assertEqual([r.workflow for r in ws.iter_rows(root)],
                         [".github/workflows/ci.yml"])
        self.assertEqual([f for f, _ in ws.ci_workflows_refs(root)],
                         [".github/workflows/ci.yml"])

    def test_checkout_root_accepts_a_worktree_git_file(self):
        root = Path(tempfile.mkdtemp()).resolve()
        (root / ".git").write_text("gitdir: /elsewhere\n")
        (root / "a" / "b").mkdir(parents=True)
        self.assertEqual(ws.checkout_root(root / "a" / "b"), root)


if __name__ == "__main__":
    unittest.main()
