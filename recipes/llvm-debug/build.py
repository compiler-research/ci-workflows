#!/usr/bin/env python3
"""Builds a Debug (-O0 -g) Clang/LLVM install tree from release/{version}.x.

Same shape as recipes/llvm-release/build.py, with two differences:
  * CMAKE_BUILD_TYPE=Debug instead of Release (assertions stay on).
  * the LLVM test utilities FileCheck and `not` are built and copied
    into the install, so a consumer's lit suite resolves them under the
    recipe install prefix -- a from-source debug LLVM has them in
    build/bin, but install-distribution doesn't ship test tools.

The shared install-tree publish flow (env validation, .o cleanup,
LLVM_DISTRIBUTION_COMPONENTS, install-distribution, find_package smoke)
lives in actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (release/{version}.x).

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built
"""

from __future__ import annotations

import os
import stat
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402

# LLVM utilities a downstream lit suite needs but install-distribution
# does not ship (they are test tools, not library/dist components).
# clang-repl rides along because it is cheap once clangInterpreter is
# built and some consumers exercise the interpreter driver.
_INSTALLED_TOOLS = ["FileCheck", "not", "clang-repl"]


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    # The source.branch_template assumes an integer-major release
    # (release/{version}.x); refuse anything else before any git op.
    try:
        int(version.split('.')[0])
    except ValueError:
        print(
            f"::error::recipe llvm-debug expects an integer-like version "
            f"(e.g. '22' or '22.1'); got '{version}'",
            file=sys.stderr,
        )
        return 1

    os.chdir(work_dir)
    llvm_build.clone_shallow(
        "https://github.com/llvm/llvm-project.git",
        f"release/{version}.x",
        work_dir / "llvm-project",
    )
    src_commit = llvm_build.record_src_commit(work_dir / "llvm-project")

    build_dir = work_dir / "llvm-project" / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    cmake_args = (
        llvm_build.base_cmake_args(str(out_dir / "install"), build_type="Debug")
        + ["-DLLVM_ENABLE_PROJECTS=clang"]
        + llvm_build.dylib_flags()
        + llvm_build.cmake_extra()
        + ["../llvm"]
    )
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore",
         *_INSTALLED_TOOLS],
        check=True,
    )

    llvm_build.cleanup_intermediates()
    llvm_build.install_distribution()

    # FileCheck / not / clang-repl register install() rules under the
    # default "Unspecified" component, so they can't ride the
    # distribution components install_distribution() drives. Copy them
    # into bin/ by hand (same approach llvm-release uses for
    # llvm-jitlink-executor) so consumers find them next to clang.
    for tool in _INSTALLED_TOOLS:
        src = build_dir / "bin" / tool
        if not src.is_file():
            print(f"build.py: warning: {tool} not found at {src}; "
                  "consumer lit suites relying on it will fail.",
                  file=sys.stderr)
            continue
        dst = out_dir / "install" / "bin" / tool
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    llvm_build.smoke()

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
