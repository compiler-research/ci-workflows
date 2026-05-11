#!/usr/bin/env python3
"""Fetch a recipe's bootstrap cell from the cache and print its bin/.

A recipe declares a cross-recipe build-time dependency by adding a
top-level `bootstrap:` block to its recipe.yaml:

    bootstrap:
      recipe: llvm-release
      version: '22'

This script reads that block, computes the bootstrap cell's cache key
(via setup-recipe/compute_key.py), downloads it via cache_io to a
known location, and prints the install bin/ on stdout. The publish-
recipe action exports that path as BOOTSTRAP_CLANG_BIN, which the
recipe's build.py picks up to drive its compile.

Recipes without a bootstrap block: this script exits 0 silently and
prints nothing — publish-recipe treats an empty BOOTSTRAP_CLANG_BIN
as "no bootstrap required" (the asan and release recipes use that).

Args: RECIPE_DIR OS ARCH [DOWNLOAD_DIR]

  RECIPE_DIR     Path to the recipe directory (contains recipe.yaml).
  OS, ARCH       Target OS/arch slugs for the cache key (must match
                 what publish-recipe used when warming the bootstrap
                 cell — same os/arch as the consuming recipe).
  DOWNLOAD_DIR   Where to extract the bootstrap cell. Defaults to
                 a sibling of the recipe build dir.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "actions" / "lib"))
import cache_io  # noqa: E402


def _grep_yaml_block_field(yaml_path: Path, block: str,
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


def main() -> int:
    if len(sys.argv) < 4 or len(sys.argv) > 5:
        print("usage: fetch_bootstrap.py RECIPE_DIR OS ARCH [DOWNLOAD_DIR]",
              file=sys.stderr)
        return 2

    recipe_dir = Path(sys.argv[1]).resolve()
    os_slug, arch = sys.argv[2], sys.argv[3]
    download_dir = (Path(sys.argv[4]) if len(sys.argv) == 5
                    else recipe_dir / "_bootstrap").resolve()

    yaml_path = recipe_dir / "recipe.yaml"
    if not yaml_path.is_file():
        print(f"fetch_bootstrap: no recipe.yaml at {yaml_path}",
              file=sys.stderr)
        return 1

    bootstrap_recipe = _grep_yaml_block_field(yaml_path, "bootstrap", "recipe")
    bootstrap_version = _grep_yaml_block_field(
        yaml_path, "bootstrap", "version",
    )
    if not bootstrap_recipe and not bootstrap_version:
        # No bootstrap declared. Silent no-op so the publish-recipe
        # step can pipe stdout into BOOTSTRAP_CLANG_BIN unconditionally.
        return 0
    if not (bootstrap_recipe and bootstrap_version):
        print(f"fetch_bootstrap: incomplete bootstrap block in {yaml_path}: "
              f"need both 'recipe' and 'version'", file=sys.stderr)
        return 1

    # Compute the bootstrap cell's cache key off the local recipes/
    # tree -- same content-hash setup-recipe uses on consumers.
    compute_key = (REPO_ROOT / "actions" / "setup-recipe"
                   / "compute_key.py")
    result = subprocess.run(
        ["python3", str(compute_key),
         bootstrap_recipe, bootstrap_version, os_slug, arch,
         str(REPO_ROOT / "recipes")],
        check=True, capture_output=True, text=True,
    )
    # compute_key prints "key=<value>"; strip the prefix.
    key_line = result.stdout.strip()
    if not key_line.startswith("key="):
        print(f"fetch_bootstrap: unexpected compute_key output: "
              f"{key_line!r}", file=sys.stderr)
        return 1
    key = key_line[len("key="):]

    base = cache_io.resolve_cache_base(os.environ.get("RECIPE_CACHE_BASE"))
    if not cache_io.cache_probe(base, key):
        print(f"fetch_bootstrap: bootstrap cell not in cache: "
              f"{bootstrap_recipe} {bootstrap_version} {os_slug} {arch} "
              f"(key={key}). Publish that cell first; the msan recipe "
              f"depends on it.", file=sys.stderr)
        return 1

    download_dir.mkdir(parents=True, exist_ok=True)
    cache_io.cache_download(base, key, str(download_dir))

    # Cells extract as <download_dir>/install/{bin,lib,include}.
    bin_dir = download_dir / "install" / "bin"
    if not (bin_dir / "clang").is_file():
        print(f"fetch_bootstrap: bootstrap cell extracted but "
              f"{bin_dir}/clang is missing; cell layout changed?",
              file=sys.stderr)
        return 1
    print(bin_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
