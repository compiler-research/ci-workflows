"""Which recipe cell a consumer's CI row pulls -- the one place that knows.

    rows = cells.scan(checkout)            # every row, with its cells
    cells.find_row(rows, "ubu24-clang20-runtime23").primary
    cells.in_catalog(coord, cells.load_catalog())

A cell is a (recipe, version, os, arch) coordinate, passed around as a
plain dict with exactly COORD_KEYS so it can go straight into bin/repro.

Consumers never name their cell: they hand matrix values to one of our
actions through `${{ }}` expressions, and each wires them differently.
bin/gha.py evaluates those calls the way the runner would; this module
turns an evaluated call into a cell.

Extending: an action that selects its recipe *in shell* (so no
expression walk can see it) needs a resolver here --

    @resolves("setup-foo")
    def _setup_foo(inputs):
        return make_coord("foo", inputs.get("version"), inputs.get("os"),
                          inputs.get("arch")), "why, when that is None"

Composites that merely forward to a resolved action (setup-biodynamo,
setup-cuda, ...) need nothing: gha.iter_calls walks into them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import gha

REPO_ROOT = Path(__file__).resolve().parent.parent
CELLS_YAML = REPO_ROOT / "cells.yaml"

COORD_KEYS = ("recipe", "version", "os", "arch")
Coord = Dict[str, str]


# ------------------------------------------------------------------ coords


def coord_str(coord: Coord) -> str:
    return "/".join(coord[k] for k in COORD_KEYS)


def parse_coord(text: str) -> Optional[Coord]:
    """`recipe/version/os/arch` -> coord, or None if not that shape."""
    parts = text.split("/")
    if len(parts) != len(COORD_KEYS) or not all(parts):
        return None
    return dict(zip(COORD_KEYS, parts))


def os_to_arch(os_slug: str) -> str:
    """Arch for a runner slug, as setup-llvm derives it.

    macOS is the one slug that doesn't name its arch: macos-N is Apple
    Silicon by GitHub's convention, macos-N-intel the x86_64 variant.
    """
    if os_slug.startswith("macos-"):
        return "x86_64" if os_slug.endswith("-intel") else "arm64"
    if os_slug.endswith("-arm"):
        return "arm64"
    return "x86_64"


def make_coord(recipe: Optional[str], version: Optional[str],
               os_slug: Optional[str],
               arch: Optional[str] = None) -> Optional[Coord]:
    """A complete coord, arch derived from os when not given; else None."""
    if not (recipe and version and os_slug):
        return None
    return {"recipe": recipe, "version": version, "os": os_slug,
            "arch": arch or os_to_arch(os_slug)}


# ----------------------------------------------------------------- catalog


def load_catalog(path: Optional[Path] = None) -> List[Coord]:
    """cells.yaml's `cells:` list: what the recipe cache holds.

    [] on a missing or unreadable file -- callers treat the catalog as
    a hint and say "not in cells.yaml" rather than crash.
    """
    path = path or CELLS_YAML
    try:
        doc = gha.load_yaml(path)
    except (OSError, gha.YAMLError, IndexError):
        return []
    out: List[Coord] = []
    for c in (doc or {}).get("cells") or []:
        if isinstance(c, dict) and all(c.get(k) not in (None, "")
                                       for k in COORD_KEYS):
            out.append({k: gha.to_str(c[k]) for k in COORD_KEYS})
    return out


def in_catalog(coord: Coord, catalog: List[Coord]) -> bool:
    return any(all(coord[k] == c[k] for k in COORD_KEYS) for c in catalog)


# --------------------------------------------------------------- resolvers

#: (cell, note). The note says why there is no cell when there is none.
Resolution = Tuple[Optional[Coord], str]
RESOLVERS: Dict[str, Callable[[Dict[str, str]], Resolution]] = {}


def resolves(action: str):
    """Register the resolver for one of our actions (see module doc)."""
    def register(fn):
        RESOLVERS[action] = fn
        return fn
    return register


@resolves("setup-recipe")
def _setup_recipe(inputs: Dict[str, str]) -> Resolution:
    coord = make_coord(inputs.get("recipe"), inputs.get("version"),
                       inputs.get("os"), inputs.get("arch"))
    return coord, "" if coord else "setup-recipe inputs incomplete"


#: setup-llvm's "Resolve flavor -> recipe" step, which runs in shell.
#: system -> None: LLVM comes from apt/brew and there is no cell.
FLAVOR_TO_RECIPE: Dict[str, Optional[str]] = {
    "":       "llvm-release",
    "asan":   "llvm-asan",
    "msan":   "llvm-msan",
    "cling":  "llvm-root",
    "debug":  "llvm-debug",
    "system": None,
}


@resolves("setup-llvm")
def _setup_llvm(inputs: Dict[str, str]) -> Resolution:
    flavor = inputs.get("flavor", "")
    if flavor not in FLAVOR_TO_RECIPE:
        return None, f"setup-llvm: unknown flavor {flavor!r}"
    recipe = FLAVOR_TO_RECIPE[flavor]
    if recipe is None:
        return None, ("setup-llvm flavor=system installs LLVM from the "
                      "system package manager")
    coord = make_coord(recipe,
                       inputs.get("flavor-version") or inputs.get("version"),
                       inputs.get("os"), inputs.get("arch"))
    return coord, "" if coord else "setup-llvm inputs incomplete"


# -------------------------------------------------------------------- scan


@dataclass
class RowCells:
    """One CI row and the cells its steps fetch, in step order."""
    workflow: str
    job: str
    row: str
    cells: List[Tuple[str, Coord]] = field(default_factory=list)  # (via, coord)
    notes: List[str] = field(default_factory=list)

    @property
    def primary(self) -> Optional[Coord]:
        """The row's toolchain: its first cell that is not an add-on.

        None for a row whose toolchain comes from apt/brew even if it
        pulls an add-on -- a cuda-headers-only devshell has no compiler
        the row would use.
        """
        return next((dict(c) for _via, c in self.cells
                     if c["recipe"] not in ADDON_RECIPES), None)


def scan(checkout: Path) -> List[RowCells]:
    """Every row of the checkout's workflows, with the cells it pulls.

    Rows that call none of our actions are left out; a row that calls
    one but gets no cell (flavor=system) stays, with a note saying so.
    """
    out: List[RowCells] = []
    for row in gha.iter_rows(checkout):
        rc = RowCells(workflow=row.workflow, job=row.job, row=row.name)
        for call in gha.iter_calls(row.steps, row.ctx, checkout, RESOLVERS):
            coord, note = RESOLVERS[call.action](call.inputs)
            if coord is None:
                rc.notes.append(note)
            elif all(coord != c for _via, c in rc.cells):
                rc.cells.append((call.via, coord))
        if rc.cells or rc.notes:
            out.append(rc)
    return out


def find_row(rows: List[RowCells], name: str) -> Optional[RowCells]:
    return next((r for r in rows if r.row == name), None)


def scan_cwd() -> List[RowCells]:
    """scan() of the checkout containing the current directory."""
    top = gha.checkout_root(Path.cwd())
    return scan(top) if top else []


# ----------------------------------------------------------------- ranking

#: Recipes a row layers on top of its toolchain, never one on their own.
ADDON_RECIPES = {"cuda-headers"}
#: Toolchains that are a variant of a plain one.
VARIANT_RECIPES = {"llvm-asan", "llvm-msan", "llvm-debug"}


def plainest_first(coord: Coord, nrows: int) -> Tuple[int, int, int]:
    """Sort key for offering cells to a newcomer: a plain llvm-release
    before other toolchains before variants, then the cell more rows
    use, then the newest version."""
    variant = (0 if coord["recipe"] == "llvm-release" else
               2 if coord["recipe"] in VARIANT_RECIPES else 1)
    digits = "".join(ch for ch in coord["version"].split(".")[0]
                     if ch.isdigit())
    return variant, -nrows, -(int(digits) if digits else 0)
