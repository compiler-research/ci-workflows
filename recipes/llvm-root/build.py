#!/usr/bin/env python3
"""Builds an LLVM/Clang install tree with cling integrated as an
LLVM_EXTERNAL_PROJECT. Source repos and branches are read from
recipe.yaml so a tag bump there is the single point of edit.

Recipe-specific bits live here (source clones, cmake flags, ninja
targets, cling-specific post-install). The shared install-tree publish
flow (env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS,
install-distribution, find_package smoke) lives in
actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         flavor selector. Substitutes into
                         recipe.yaml's source.branch_template
                         ({version} → branch). Today: 'cling-llvm20'
                         or 'ROOT-llvm20'. Factored into the cache
                         key so each flavor has its own asset.

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


def _grep_yaml(path: Path, key: str, occurrence: int = 1) -> Optional[str]:
    """Return the Nth occurrence (1-indexed) of `<key>: value` in path.

    Same shape as the bash predecessor's grep+sed pipeline; not a
    general YAML parser. Used to surface source.repo / source.branch_template
    plus the second `repo:` (cling) and the first `branch:` (cling).
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$")
    matches = []
    for line in path.read_text().splitlines():
        m = pattern.match(line)
        if m:
            matches.append(m.group(1).strip().strip('"').strip("'"))
    if len(matches) >= occurrence:
        return matches[occurrence - 1]
    return None


def _install_executable(src: Path, dst: Path) -> None:
    """Copy and chmod +x. Replaces `install -m 0755` (GNU coreutils)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _try_install_component(build_dir: Path, comp: str) -> bool:
    """`cmake --install . --component <comp>`; return True if it ran clean.

    cling's components don't have install-X umbrella targets (cling's
    CMakeLists uses raw install(TARGETS) rather than LLVM's
    add_llvm_install_targets), so we install them directly via cmake.
    Returns False on failure so the caller can fall back to a manual copy.
    """
    result = subprocess.run(
        ["cmake", "--install", str(build_dir), "--component", comp],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"build.py: cling component {comp} install rule absent",
              file=sys.stderr)
        return False
    return True


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    # Pull source coordinates from recipe.yaml. Mirrors the bash
    # predecessor's grep approach — keeps recipe.yaml the single
    # source of truth without a YAML-parser dependency.
    yaml_path = SCRIPT_DIR / "recipe.yaml"
    llvm_repo       = _grep_yaml(yaml_path, "repo",            occurrence=1)
    llvm_branch_tpl = _grep_yaml(yaml_path, "branch_template", occurrence=1)
    cling_repo      = _grep_yaml(yaml_path, "repo",            occurrence=2)
    cling_branch    = _grep_yaml(yaml_path, "branch",          occurrence=1)

    for name, value in (("source.repo",            llvm_repo),
                        ("source.branch_template", llvm_branch_tpl),
                        ("cling.repo",             cling_repo),
                        ("cling.branch",           cling_branch)):
        if not value:
            print(f"build.py: {name} missing in recipe.yaml", file=sys.stderr)
            return 1

    # {version} → flavor name. Same substitution build_manifest.py does.
    llvm_branch = llvm_branch_tpl.replace("{version}", version)
    print(f"build.py: flavor={version}; cloning {llvm_repo}@{llvm_branch}",
          flush=True)

    os.chdir(work_dir)
    llvm_build.clone_shallow(cling_repo, cling_branch, work_dir / "cling")
    llvm_build.clone_shallow(llvm_repo, llvm_branch, work_dir / "llvm-project")
    src_commit = llvm_build.record_src_commit(work_dir / "llvm-project")

    build_dir = work_dir / "llvm-project" / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    # LLVM_EXTERNAL_PROJECTS=cling pulls cling's CMakeLists into the same
    # build tree. cling's libraries get added to the build but cling uses
    # raw install(TARGETS) rather than LLVM's add_llvm_install_targets,
    # so its components have no install-X umbrellas — they're installed
    # separately below via cmake --install --component.
    cmake_args = (
        llvm_build.base_cmake_args(str(out_dir / "llvm-project"))
        + [
            '-DLLVM_ENABLE_PROJECTS=clang',
            '-DLLVM_EXTERNAL_PROJECTS=cling',
            f'-DLLVM_EXTERNAL_CLING_SOURCE_DIR={work_dir / "cling"}',
        ]
        + llvm_build.cmake_extra()
        + ["../llvm"]
    )
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    # Build clang driver + clang-repl Interpreter library (downstream ROOT
    # consumes libclangInterpreter.a even though the clang driver doesn't
    # depend on it transitively) + StaticAnalyzerCore (cling-bundled clang
    # pulled it into CppInterOp's link in the past) + LLVMOrcDebugging
    # (cling pulls it via LIBS) + clingInterpreter (the cling library;
    # the `cling` binary follows transitively).
    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore",
         "LLVMOrcDebugging", "clingInterpreter"],
        check=True,
    )

    llvm_build.cleanup_intermediates()

    # Cling components must NOT be in DIST_COMPONENTS — install-distribution
    # requires install-X umbrellas that cling's raw install(TARGETS)
    # doesn't create. Install LLVM/clang via the helper here; cling
    # follows below.
    llvm_build.install_distribution()

    # Walk libcling*.a / cling*.lib (Windows MSVC convention) and install
    # whatever cling produced via cmake --install --component.
    lib = Path("lib")
    if lib.is_dir():
        for p in sorted(lib.iterdir()):
            n = p.name
            if not (n.startswith(("libcling", "cling"))
                    and (n.endswith(".a") or n.endswith(".lib"))):
                continue
            comp = n[3:] if n.startswith("lib") else n
            comp = comp[:-2] if comp.endswith(".a") else comp[:-4]
            _try_install_component(build_dir, comp)

    # `cling` binary: cling's CMakeLists install(TARGETS cling RUNTIME ...)
    # uses COMPONENT cling. If that path doesn't exist (older cling),
    # fall back to a manual copy.
    if not _try_install_component(build_dir, "cling"):
        for fallback in ("bin/cling", "bin/cling.exe"):
            src = build_dir / fallback
            if src.is_file():
                _install_executable(src, out_dir / "llvm-project" / fallback)
                break

    # Cling cmake-exports: ships ClingConfig.cmake + ClingTargets.cmake
    # under lib/cmake/cling/ so downstream consumers can resolve cling
    # via `find_package(Cling REQUIRED CONFIG)` (CppInterOp does this).
    # The lib-walk above doesn't pick this up: the component installs
    # cmake files, not a libcling*.a, so its name doesn't appear there.
    _try_install_component(build_dir, "cling-cmake-exports")

    # Cling headers — cling's install rules typically don't ship them
    # (consumers historically read from the source tree). Stage them
    # under include/cling/ in the install tree so consumers find them
    # without an extra source clone.
    cling_headers = work_dir / "cling" / "include" / "cling"
    if cling_headers.is_dir():
        dst = out_dir / "llvm-project" / "include" / "cling"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(cling_headers, dst)

    # Producer-side smoke. find_package(LLVM)+(Clang) covers LLVM/clang;
    # require ClingConfig.cmake + libclingInterpreter for cling so a
    # missing cmake-exports install rule fails loudly here instead of
    # surfacing as a downstream `find_package(Cling)` failure on every
    # consumer run.
    install = out_dir / "llvm-project"
    cling_cfg = "lib/cmake/cling/ClingConfig.cmake"
    if (install / "lib" / "libclingInterpreter.a").is_file():
        llvm_build.smoke(required_files=["lib/libclingInterpreter.a",
                                         cling_cfg])
    elif (install / "lib" / "clingInterpreter.lib").is_file():
        llvm_build.smoke(required_files=["lib/clingInterpreter.lib",
                                         cling_cfg])
    else:
        print("::error::neither libclingInterpreter.a nor "
              "clingInterpreter.lib found in install", file=sys.stderr)
        return 1

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
