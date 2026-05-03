#!/usr/bin/env python3
"""Emit the JSON manifest describing a published recipe build.

Args: RECIPE VERSION OS ARCH KEY
Reads (env):
  SRC_COMMIT             set by the recipe's build script
  GITHUB_SHA             the ci-workflows commit being built from
  ImageOS, ImageVersion  runner image identifiers (GHA-injected)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _grep_yaml_value(yaml_path: Path, key: str) -> Optional[str]:
    """Extract `<key>: value` from a YAML file (no parser needed for the
    handful of fields we surface). Returns first match or None.

    Same shape as the bash predecessor's grep+sed pipeline; not a
    general YAML parser.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$")
    try:
        for line in yaml_path.read_text().splitlines():
            m = pattern.match(line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _cmake_state() -> dict:
    """Snapshot of cmake's configured-state files from the build dir.

    Captures the producer's cmake-probe output so consumers can detect
    environment drift before iterating. Three files:
      CMakeCache.txt           top-level vars (CMAKE_CXX_COMPILER,
                               _STANDARD, _FLAGS, _COMPILER_LAUNCHER, ...)
      CMakeCXXCompiler.cmake   compiler probe + IMPLICIT_INCLUDE_DIRECTORIES
                               (the libstdc++ resolution that decides
                               what /usr/include/c++/N is loaded)
      CMakeCCompiler.cmake     same for C
    The content lets consumers diff against their own equivalents and
    fail loudly on mismatches -- the libstdc++-13 vs libstdc++-14 drift
    that produced 100% ccache miss in the catthehacker/ubuntu container
    would have surfaced as a one-line diff against
    CMAKE_CXX_IMPLICIT_INCLUDE_DIRECTORIES.
    """
    work = os.environ.get("WORK_DIR", "")
    if not work:
        return {}
    workp = Path(work)
    out: dict[str, str] = {}
    cache = next(workp.glob("**/build/CMakeCache.txt"), None)
    if cache:
        try:
            out["CMakeCache.txt"] = cache.read_text()
        except OSError:
            pass
    for name in ("CMakeCXXCompiler.cmake", "CMakeCCompiler.cmake"):
        for p in workp.glob(f"**/CMakeFiles/*/{name}"):
            try:
                out[name] = p.read_text()
                break
            except OSError:
                continue
    return out


def _installed_packages() -> dict:
    """Map {pkg-name: version} of packages installed on the producer.

    Recorded so consumers can diff against their local package set and
    apt-install whatever's missing. Catches any divergence cmake's
    IMPLICIT_INCLUDE doesn't surface (zlib-dev, libedit-dev, libtinfo,
    ...) plus the libstdc++ case (libstdc++-N-dev). dpkg-query is
    debian-only; the empty fallback on macOS/Windows is fine since the
    runtime-package class of ccache-miss only happens on Linux.
    """
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            name, ver = line.split("\t", 1)
            out[name] = ver
    return out


def _ccache_config() -> dict:
    """Snapshot the ccache knobs that decide off-runner reuse."""
    keys = ("compiler_check", "hash_dir", "base_dir")
    out: dict[str, str] = {}
    for k in keys:
        try:
            r = subprocess.run(
                ["ccache", "--get-config", k],
                check=True, capture_output=True, text=True,
            )
            out[k] = r.stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            out[k] = "unknown"
    return out


def _cmake_args() -> list:
    """cmake invocation written by llvm_build.record_cmake_args.

    Empty list = not recorded (older recipe or no WORK_DIR), not "no flags".
    """
    work = os.environ.get("WORK_DIR", "")
    if not work:
        return []
    p = Path(work) / "cmake-args.json"
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _build_script(recipe_dir: Path) -> Optional[Path]:
    sh = recipe_dir / "build.sh"
    py = recipe_dir / "build.py"
    if sh.is_file():
        return sh
    if py.is_file():
        return py
    return None


def build_manifest(recipe: str, version: str, os_: str, arch: str,
                   key: str, recipe_root: str = "recipes") -> dict:
    """Return the manifest as a dict; caller json-encodes."""
    recipe_dir = Path(recipe_root) / recipe
    yaml_path = recipe_dir / "recipe.yaml"

    recipe_yaml_sha = _file_sha(yaml_path) if yaml_path.is_file() else "unknown"
    bs = _build_script(recipe_dir)
    build_script_sha = _file_sha(bs) if bs is not None else "unknown"
    build_script_name = bs.name if bs is not None else "unknown"

    repo = _grep_yaml_value(yaml_path, "repo") or "unknown"
    branch_tpl = _grep_yaml_value(yaml_path, "branch_template") or ""
    branch = branch_tpl.replace("{version}", version) if branch_tpl else "unknown"

    return {
        "key": key,
        "recipe": recipe,
        "version": version,
        "platform": {
            "os": os_,
            "arch": arch,
            "runner_image":         os.environ.get("ImageOS", "unknown"),
            "runner_image_version": os.environ.get("ImageVersion", "unknown"),
        },
        "recipe_yaml_sha256": recipe_yaml_sha,
        "build_script": build_script_name,
        "build_script_sha256": build_script_sha,
        # Backward-compat alias for tooling that read the old field name.
        "build_sh_sha256": build_script_sha,
        "source": {
            "repo":   repo,
            "branch": branch,
            "commit": os.environ.get("SRC_COMMIT", "unknown"),
        },
        # Toolchain + ccache config consumers (bin/repro --devshell)
        # need to replicate to hit the sibling cache.
        "build_env": {
            "cc":  os.environ.get("CC",  "unknown"),
            "cxx": os.environ.get("CXX", "unknown"),
            "ccache": _ccache_config(),
            "installed_packages": _installed_packages(),
        },
        "cmake_args": _cmake_args(),
        "cmake_state": _cmake_state(),
        "ci_workflows_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _main(argv: list[str]) -> int:
    if len(argv) not in (5, 6):
        print("usage: build_manifest.py RECIPE VERSION OS ARCH KEY [recipe_root]",
              file=sys.stderr)
        return 2
    recipe_root = argv[5] if len(argv) == 6 else "recipes"
    manifest = build_manifest(*argv[:5], recipe_root=recipe_root)
    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
