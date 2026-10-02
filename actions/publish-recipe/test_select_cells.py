"""Unit tests for select_cells: what a publish-recipe dispatch builds."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import select_cells as sc

CELLS = [
    {"recipe": "llvm-asan", "version": "23", "os": "ubuntu-24.04", "arch": "x86_64"},
    {"recipe": "llvm-msan", "version": "23", "os": "ubuntu-24.04", "arch": "x86_64"},
    {"recipe": "llvm-release", "version": "23.1.0-rc2", "os": "ubuntu-24.04", "arch": "x86_64"},
    {"recipe": "llvm-release", "version": "23", "os": "ubuntu-24.04", "arch": "x86_64"},
    {"recipe": "llvm-release", "version": "23", "os": "macos-26", "arch": "arm64"},
    {"recipe": "llvm-release", "version": "22", "os": "macos-26", "arch": "arm64"},
]


def coords(cells):
    return [sc.coord(c) for c in cells]


class SelectTest(unittest.TestCase):
    def test_star_stays_within_its_part(self):
        got, bad = sc.select(CELLS, ["*/23/*/*"])
        self.assertEqual(bad, [])
        self.assertEqual(coords(got), [
            "llvm-asan/23/ubuntu-24.04/x86_64", "llvm-msan/23/ubuntu-24.04/x86_64",
            "llvm-release/23/ubuntu-24.04/x86_64", "llvm-release/23/macos-26/arm64"])

    def test_patterns_without_four_parts_match_nothing(self):
        for p in ("*/23/*", "*23*", "llvm-release/23/*/*/x"):
            self.assertEqual(sc.select(CELLS, [p]), ([], [p]), p)

    def test_overlapping_patterns_keep_cells_yaml_order_once(self):
        got, _ = sc.select(CELLS, ["llvm-release/23/macos-*/*", "*/23/*/*"])
        self.assertEqual(len(got), 4)
        self.assertEqual(coords(got)[0], "llvm-asan/23/ubuntu-24.04/x86_64")

    def test_unmatched_patterns_are_reported(self):
        _, bad = sc.select(CELLS, ["llvm-releese/23/*/*", "*/22/*/*"])
        self.assertEqual(bad, ["llvm-releese/23/*/*"])

    def test_parse_patterns(self):
        self.assertEqual(sc.parse_patterns(" a/b/c/d,e/f/g/h\n i/j/k/l "),
                         ["a/b/c/d", "e/f/g/h", "i/j/k/l"])

    def test_bootstrapping_recipes_build_second(self):
        first, after = sc.split(CELLS[:2], lambda r: r == "llvm-msan")
        self.assertEqual(coords(first), ["llvm-asan/23/ubuntu-24.04/x86_64"])
        self.assertEqual(coords(after), ["llvm-msan/23/ubuntu-24.04/x86_64"])

    def test_has_bootstrap_reads_the_recipe(self):
        root = Path(__file__).resolve().parents[2]
        self.assertTrue(sc.has_bootstrap("llvm-msan", root))
        self.assertFalse(sc.has_bootstrap("llvm-release", root))


class MainTest(unittest.TestCase):
    def _run(self, **env):
        f = Path(tempfile.mkdtemp()) / "cells.json"
        f.write_text(json.dumps(CELLS))
        base = {k: "" for k in ("CELLS", "RECIPE", "VERSION", "OS", "ARCH")}
        with mock.patch.dict(os.environ, {**base, **env}), \
                mock.patch.object(sc, "has_bootstrap", lambda r: r == "llvm-msan"), \
                redirect_stdout(io.StringIO()) as out, \
                redirect_stderr(io.StringIO()) as err:
            rc = sc.main(["select_cells.py", str(f)])
        return rc, dict(l.split("=", 1) for l in out.getvalue().splitlines()), err.getvalue()

    def test_bulk_request_writes_both_matrices(self):
        rc, out, _ = self._run(CELLS="*/23/ubuntu-24.04/*")
        self.assertEqual(rc, 0)
        self.assertEqual(coords(json.loads(out["bootstrap_matrix"])["include"]), [
            "llvm-asan/23/ubuntu-24.04/x86_64", "llvm-release/23/ubuntu-24.04/x86_64"])
        self.assertEqual(out["dependent_empty"], "false")

    def test_single_cell_fields_still_work(self):
        rc, out, _ = self._run(RECIPE="llvm-release", VERSION="23",
                               OS="macos-26", ARCH="arm64")
        self.assertEqual(rc, 0)
        self.assertEqual(out["dependent_empty"], "true")

    def test_a_typo_fails_and_lists_the_cells(self):
        rc, _, err = self._run(CELLS="llvm-releese/23/*/*")
        self.assertEqual(rc, 1)
        self.assertIn("llvm-releese/23/*/*", err)
        self.assertIn("llvm-release/23/macos-26/arm64", err)

    def test_an_empty_request_fails(self):
        self.assertEqual(self._run()[0], 1)


if __name__ == "__main__":
    unittest.main()
