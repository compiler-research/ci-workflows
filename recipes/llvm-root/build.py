#!/usr/bin/env python3
"""Builds an LLVM/Clang install tree against root-project/llvm-project's
cling-llvm20 / ROOT-llvm20 branches. Source repo and branch template
are read from recipe.yaml so a tag bump there is the single point of
edit.

Recipe-specific bits live here (source clone, cmake flags, ninja
targets). The shared install-tree publish flow (env validation,
.o cleanup, LLVM_DISTRIBUTION_COMPONENTS, install-distribution,
find_package smoke) lives in actions/lib/llvm_build.py.

Cling is intentionally *not* bundled -- consumers rebuild it on top
of this install. See recipe.yaml for the why.

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
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


def _grep_yaml(path: Path, key: str) -> Optional[str]:
    """Return the first `<key>: value` in path. Hand-rolled (not a
    YAML parser) because adding PyYAML for two scalar lookups isn't
    worth the dep."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*$")
    for line in path.read_text().splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    yaml_path = SCRIPT_DIR / "recipe.yaml"
    llvm_repo       = _grep_yaml(yaml_path, "repo")
    llvm_branch_tpl = _grep_yaml(yaml_path, "branch_template")
    for name, value in (("source.repo",            llvm_repo),
                        ("source.branch_template", llvm_branch_tpl)):
        if not value:
            print(f"build.py: {name} missing in recipe.yaml", file=sys.stderr)
            return 1

    # {version} → flavor name. Same substitution build_manifest.py does.
    llvm_branch = llvm_branch_tpl.replace("{version}", version)
    print(f"build.py: flavor={version}; cloning {llvm_repo}@{llvm_branch}",
          flush=True)

    os.chdir(work_dir)
    llvm_build.clone_shallow(llvm_repo, llvm_branch, work_dir / "llvm-project")
    src_commit = llvm_build.record_src_commit(work_dir / "llvm-project")

    build_dir = work_dir / "llvm-project" / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    cmake_args = (
        llvm_build.base_cmake_args(str(out_dir / "llvm-project"))
        + ['-DLLVM_ENABLE_PROJECTS=clang']
        + llvm_build.cmake_extra()
        + ["../llvm"]
    )
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    # Build clang driver + clang-repl Interpreter library (downstream
    # ROOT consumes libclangInterpreter.a even though the clang driver
    # doesn't depend on it transitively) + StaticAnalyzerCore (cling's
    # bundled clang pulled it into CppInterOp's link in the past) +
    # LLVMOrcDebugging (cling pulls it via LIBS) + LLVMLineEditor
    # (cling's UserInterface declares it via LLVM_LINK_COMPONENTS).
    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore",
         "LLVMOrcDebugging", "LLVMLineEditor"],
        check=True,
    )

    llvm_build.cleanup_intermediates()
    llvm_build.install_distribution()

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
