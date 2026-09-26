#!/usr/bin/env python3
"""The `bootstrap:` block of a recipe, read without a YAML parser.

Two things need it and must agree: fetch_bootstrap.py, to download the
cell a recipe is built on, and compute_key.py, to fold that cell's key
into the dependent's. Read by two parsers they would drift, and the
consequence is silent -- a key that does not move when the thing it was
built with does.

Deliberately not under actions/lib/: every .py there is hashed into every
recipe's key, so a module added for one recipe would republish them all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple


def grep_yaml_block_field(yaml_path: Path, block: str,
                           field: str) -> Optional[str]:
    """Return `<block>.<field>` value from a YAML file (no parser).

    Tolerates two-space indented field lines under a top-level block
    that ends with ':'. Same shape as build_manifest.py's
    _grep_yaml_value, extended to one level of nesting.
    """
    try:
        text = yaml_path.read_text()
    except OSError:
        return None
    in_block = False
    block_re = re.compile(rf"^\s*{re.escape(block)}\s*:\s*$")
    field_re = re.compile(rf"^\s+{re.escape(field)}\s*:\s*(.*?)\s*$")
    top_re = re.compile(r"^[A-Za-z_]+\s*:")
    for line in text.splitlines():
        if in_block and top_re.match(line):
            in_block = False
        if block_re.match(line):
            in_block = True
            continue
        if in_block:
            m = field_re.match(line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None


def resolve_bootstrap_version(declared: str, recipe_version: str) -> str:
    """Substitute the consuming cell's version into `declared`.

    A literal ('22') passes through untouched, so recipes pinning one
    bootstrap regardless of their own version keep working.
    """
    return declared.replace("{version}", recipe_version)


def bootstrap_cell(recipe_dir: Path, recipe_version: str
                   ) -> Optional[Tuple[str, str]]:
    """The (recipe, version) a recipe bootstraps from, or None for most.

    Raises on a half-written block rather than treating it as absent: a
    bootstrap named without a version is a mistake, and ignoring it would
    hand back a key that claims no bootstrap was involved.
    """
    yaml_path = recipe_dir / "recipe.yaml"
    recipe = grep_yaml_block_field(yaml_path, "bootstrap", "recipe")
    version = grep_yaml_block_field(yaml_path, "bootstrap", "version")
    if recipe is None and version is None:
        return None
    if not recipe or not version:
        raise ValueError(
            f"bootstrap_cell: incomplete bootstrap block in {yaml_path}: "
            f"recipe={recipe!r} version={version!r}")
    return recipe, resolve_bootstrap_version(version, recipe_version)
