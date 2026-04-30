#!/usr/bin/env python3
"""Compute a deterministic cache key for a recipe build.

Args: RECIPE VERSION OS ARCH [recipe_root]

`recipe_root` defaults to "recipes/" relative to cwd (i.e. expects to
be invoked from the ci-workflows repo root). Pass an explicit path
when invoked from elsewhere.

Output: a single line `key=<recipe>-<version>-<os>-<arch>-<short-hash>`
suitable for appending to $GITHUB_OUTPUT.

Hash inputs that *should* invalidate when changed:
  - recipe.yaml  (declarative metadata)
  - build.sh or build.py  (imperative build; whichever exists)
  - patches/**   (any local patches applied to the source)
  - actions/lib/**.py  (shared build helpers — install-distribution
    component list, smoke checks. Reshape these and the published
    artifact reshapes too; the key must move so old cells stop
    shadowing new code. Test files and __pycache__ are excluded.)
  - the literal version/os/arch tuple

What we deliberately do NOT include:
  - runner image SHA — bumps shouldn't invalidate every cell. Image
    details land in the manifest for forensics.
  - timestamps — keys must be reproducible.
  - file *paths* — only contents are hashed (matches the bash predecessor's
    `sha256sum < FILE` form), so the key is the same whether called
    with a relative or absolute recipe_root. Patch filenames are part
    of the per-patch line so a renamed patch invalidates but moving
    the patches dir does not.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional


def _file_hash(path: Path) -> str:
    """SHA-256 hex of file contents (no path/metadata)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_script(recipe_dir: Path) -> Path:
    """Return the recipe's build script: prefer build.sh, fall back to build.py.

    Both shouldn't coexist — if they do, build.sh wins (matches the
    bash predecessor's behavior so existing keys don't shift). When a
    recipe migrates fully to Python it loses build.sh and we hash
    build.py; the key changes by design.
    """
    sh = recipe_dir / "build.sh"
    if sh.is_file():
        return sh
    py = recipe_dir / "build.py"
    if py.is_file():
        return py
    raise FileNotFoundError(
        f"compute_key: no build.sh or build.py in {recipe_dir}"
    )


def _lib_hash_lines(lib_root: Path) -> list[str]:
    """Per-file hash lines for non-test Python under actions/lib/.

    Returns sorted "lib/<relpath> <hex>\\n" entries; empty list when
    lib_root is absent (consumer-style invocations from outside the
    repo don't carry the build helpers and don't need them in the key).
    test_*.py and __pycache__ are skipped: tests don't shape output,
    and the bytecode dir is build-time only.
    """
    if not lib_root.is_dir():
        return []
    rel_files = []
    for p in lib_root.rglob("*.py"):
        if not p.is_file() or p.name.startswith("test_") \
                or "__pycache__" in p.parts:
            continue
        rel = "lib/" + p.relative_to(lib_root).as_posix()
        rel_files.append((rel, p))
    rel_files.sort(key=lambda x: x[0])
    return [f"{rel} {_file_hash(path)}\n" for rel, path in rel_files]


def compute_key(recipe: str, version: str, os_: str, arch: str,
                recipe_root: str = "recipes",
                lib_root: str = "actions/lib") -> str:
    """Return the full cache key for the given (recipe, version, os, arch)."""
    recipe_dir = Path(recipe_root) / recipe
    if not recipe_dir.is_dir():
        raise FileNotFoundError(
            f"compute_key: recipe directory not found: {recipe_dir}"
        )

    parts = []
    parts.append(_file_hash(recipe_dir / "recipe.yaml") + "\n")
    parts.append(_file_hash(_build_script(recipe_dir)) + "\n")

    patches_dir = recipe_dir / "patches"
    if patches_dir.is_dir():
        # relpath with `./` prefix and forward slashes matches
        # `find . -type f` output under LC_ALL=C sort.
        rel_files = []
        for p in patches_dir.rglob("*"):
            if p.is_file():
                rel = "./" + p.relative_to(patches_dir).as_posix()
                rel_files.append((rel, p))
        rel_files.sort(key=lambda x: x[0])
        for rel, path in rel_files:
            parts.append(f"{rel} {_file_hash(path)}\n")

    parts.extend(_lib_hash_lines(Path(lib_root)))

    parts.append(f"V={version} OS={os_} ARCH={arch}\n")

    full = "".join(parts).encode("utf-8")
    short = hashlib.sha256(full).hexdigest()[:16]
    return f"{recipe}-{version}-{os_}-{arch}-{short}"


def _main(argv: list[str]) -> int:
    if len(argv) < 4 or len(argv) > 6:
        print("usage: compute_key.py RECIPE VERSION OS ARCH "
              "[recipe_root [lib_root]]",
              file=sys.stderr)
        return 2
    recipe, version, os_, arch = argv[:4]
    recipe_root = argv[4] if len(argv) >= 5 else "recipes"
    lib_root = argv[5] if len(argv) >= 6 else "actions/lib"
    key = compute_key(recipe, version, os_, arch, recipe_root, lib_root)
    print(f"key={key}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
