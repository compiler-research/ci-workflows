#!/usr/bin/env python3
"""The patches a recipe applies to a version, read without a YAML parser.

Two things need them and must agree: the recipe's build, to apply them,
and compute_key.py, to hash them. Chosen by two pieces of code they would
drift, and the consequence is silent -- a key that does not move when a
patch the build applies does.

A recipe whose patches are per LLVM major names them in recipe.yaml:

  patches:
    glob: 'emscripten-clang{major}-*.patch'

where `{major}` is the cell's version up to its first '-'. Without the
block, every file under patches/ applies.

Deliberately not under actions/lib/: every .py there is hashed into every
recipe's key, so a module added for one recipe would republish them all.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from bootstrap_block import grep_yaml_block_field


def selected_patches(recipe_dir: Path, version: str) -> list[Path]:
    """The files under `recipe_dir`/patches/ a cell of `version` applies,
    sorted by their path relative to patches/."""
    patches_dir = recipe_dir / "patches"
    if not patches_dir.is_dir():
        return []
    pattern = grep_yaml_block_field(recipe_dir / "recipe.yaml",
                                    "patches", "glob")
    if pattern is not None:
        pattern = pattern.replace("{major}", version.split("-")[0])
    rels = sorted(p.relative_to(patches_dir).as_posix()
                  for p in patches_dir.rglob("*") if p.is_file())
    return [patches_dir / rel for rel in rels
            if pattern is None or fnmatch.fnmatchcase(rel, pattern)]
