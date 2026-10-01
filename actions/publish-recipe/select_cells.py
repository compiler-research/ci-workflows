"""Resolve a publish-recipe dispatch to the cells.yaml cells it names.

    select_cells.py CELLS_JSON >> "$GITHUB_OUTPUT"

CELLS_JSON is cells.yaml's `cells` list as JSON. The request comes from
the environment: CELLS, recipe/version/os/arch patterns separated by
spaces or commas, or -- when that is empty -- the single cell RECIPE,
VERSION, OS, ARCH. Prints bootstrap_matrix / dependent_matrix and their
*_empty flags; exits non-zero, listing the cells, when a pattern matches
none.

A pattern has exactly the four parts of a coordinate and is matched part
by part, so `*` never reaches across a `/`: `*/23/*/*` is every version
"23" cell and not `23.1.0-rc2`.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple

KEYS = ("recipe", "version", "os", "arch")
Cell = Dict[str, str]


def coord(cell: Cell) -> str:
    return "/".join(str(cell[k]) for k in KEYS)


def parse_patterns(text: str) -> List[str]:
    return [p for p in re.split(r"[\s,]+", text) if p]


def matches(pattern: str, cell: Cell) -> bool:
    parts = pattern.split("/")
    return len(parts) == len(KEYS) and all(
        fnmatch.fnmatchcase(str(cell[k]), p) for k, p in zip(KEYS, parts))


def select(cells: Sequence[Cell], patterns: Sequence[str]
           ) -> Tuple[List[Cell], List[str]]:
    """(matched cells in cells.yaml order, patterns that matched none)."""
    chosen: List[Cell] = []
    bad: List[str] = []
    for p in patterns:
        hit = [c for c in cells if matches(p, c)]
        if not hit:
            bad.append(p)
        chosen += [c for c in hit if c not in chosen]
    return [c for c in cells if c in chosen], bad


def has_bootstrap(recipe: str, root: Path = Path(".")) -> bool:
    """Does the recipe build with another cell's install (a top-level
    `bootstrap:` in its recipe.yaml)?"""
    text = (root / "recipes" / recipe / "recipe.yaml").read_text(encoding="utf-8")
    return re.search(r"^bootstrap:", text, re.M) is not None


def split(cells: Sequence[Cell], bootstraps: Callable[[str], bool]
          ) -> Tuple[List[Cell], List[Cell]]:
    """(cells to build first, cells that build with one of those)."""
    first, after = [], []
    for c in cells:
        (after if bootstraps(c["recipe"]) else first).append(
            {k: str(c[k]) for k in KEYS})
    return first, after


def main(argv: Sequence[str]) -> int:
    cells = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    patterns = parse_patterns(os.environ.get("CELLS", ""))
    if not patterns:
        if not os.environ.get("RECIPE"):
            print("::error::give `cells`, or recipe/version/os/arch",
                  file=sys.stderr)
            return 1
        patterns = ["/".join(os.environ.get(k, "") for k in
                             ("RECIPE", "VERSION", "OS", "ARCH"))]
    chosen, bad = select(cells, patterns)
    if bad:
        print("::error::no cell in cells.yaml matches: " + " ".join(bad)
              + " (patterns are recipe/version/os/arch)", file=sys.stderr)
        print("Cells:", *("  " + coord(c) for c in cells), sep="\n",
              file=sys.stderr)
        return 1
    for c in chosen:
        print(f"::notice::publishing {coord(c)}", file=sys.stderr)
    first, after = split(chosen, has_bootstrap)
    for name, cs in (("bootstrap", first), ("dependent", after)):
        print(f"{name}_matrix=" + json.dumps({"include": cs},
                                            separators=(",", ":")))
        print(f"{name}_empty=" + ("false" if cs else "true"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
