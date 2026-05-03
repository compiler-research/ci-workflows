#!/usr/bin/env python3
"""Builds a vanilla Clang/LLVM install tree from release/{version}.x.

Recipe-specific bits live here (source clone, cmake flags, ninja
targets, post-install hooks). The shared install-tree publish flow
(env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS,
install-distribution, find_package smoke) lives in
actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (release/{version}.x).

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built

FIXME: dedup with recipes/llvm-asan/build.py.
The compiler-rt OFF flags below, the _oop_targets() helper, and the
llvm-jitlink-executor install copy are near-verbatim copies from
llvm-asan/build.py. Lift the three into actions/lib/llvm_build.py
once a third LLVM-family recipe needs them (e.g. llvm-msan), or
sooner if the duplication starts drifting between the two recipes.
The shape ranged from "small focused helpers" to "build_llvm_release
function with kwargs that other recipes specialize"; pick at lift time.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


def _oop_targets(build_dir: Path) -> list[str]:
    """Discover orc_rt_<platform> ninja targets in the configured build.

    Mirrors llvm-asan's discovery: target name varies per platform
    (orc_rt_osx, orc_rt_linux_x86_64, orc_rt_iossim, ...). For LLVM >= 22
    compiler-rt is enabled solely for the OOP-JIT runtime that
    CppInterOp's clang-repl driver consumes; we ship it bundled in the
    artifact so consumers don't have to rebuild compiler-rt.
    """
    out = subprocess.run(
        ["ninja", "-t", "targets", "all"],
        cwd=build_dir, check=False, capture_output=True, text=True,
    ).stdout
    seen = set()
    for line in out.splitlines():
        m = re.match(r"^(orc_rt[^:]*):", line)
        if not m:
            continue
        target = m.group(1)
        # Skip the static-archive aliases ninja prints next to the
        # cmake target ("orc_rt-x86_64.lib" on Windows, "...a" on
        # Linux); only the bare target has an install rule.
        if target.endswith((".lib", ".a")):
            continue
        seen.add(target)
    return sorted(seen)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    # Parse + validate the LLVM major before any side effects.
    # The threshold (>= 22) governs the OOP-JIT compiler-rt branch;
    # the previous form silently fell back to need_oop=False on any
    # ValueError, which produced an artifact without the OOP runtime
    # when the caller passed a non-integer version (e.g. '22.1') —
    # exactly the consumers most likely to need OOP. Parse the major
    # prefix and refuse anything that doesn't yield an integer; the
    # recipe's source.branch_template also assumes integer-major
    # releases (release/{version}.x), so erroring here surfaces the
    # misuse before any git operation.
    try:
        major = int(version.split('.')[0])
    except ValueError:
        print(
            f"::error::recipe llvm-release expects an integer-like "
            f"version (e.g. '22' or '22.1'); got '{version}'",
            file=sys.stderr,
        )
        return 1
    need_oop = major >= 22

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

    # LLVM >= 22 bundles the OOP-JIT runtime; older majors don't have
    # the orc_rt targets in compiler-rt yet, so projects stay at "clang".
    # Same threshold as Build_LLVM/action.yml.
    if need_oop:
        projects = "clang;compiler-rt"
        compiler_rt_flags = [
            "-DCOMPILER_RT_BUILD_BUILTINS=OFF",
            "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF",
            "-DCOMPILER_RT_BUILD_PROFILE=OFF",
            "-DCOMPILER_RT_BUILD_MEMPROF=OFF",
            "-DCOMPILER_RT_BUILD_SANITIZERS=OFF",
            "-DCOMPILER_RT_BUILD_XRAY=OFF",
            "-DCOMPILER_RT_BUILD_GWP_ASAN=OFF",
            "-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF",
        ]
    else:
        projects = "clang"
        compiler_rt_flags = []

    cmake_args = (
        llvm_build.base_cmake_args(str(out_dir / "llvm-project"))
        + [f"-DLLVM_ENABLE_PROJECTS={projects}"]
        + compiler_rt_flags
        + llvm_build.cmake_extra()
        + ["../llvm"]
    )
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore"],
        check=True,
    )

    oop_targets: list[str] = []
    if need_oop:
        oop_targets = _oop_targets(build_dir)
        if oop_targets:
            subprocess.run(
                ["ninja", "-j", ncpus, "llvm-jitlink-executor", *oop_targets],
                check=True,
            )
        else:
            print("build.py: no orc_rt targets matched; "
                  "OOP-JIT runtime won't be in the artifact.",
                  file=sys.stderr)

    llvm_build.cleanup_intermediates()

    # Pass OOP_TARGETS as extra DIST_COMPONENTS so install-distribution
    # ships them and LLVMExports.cmake stays self-consistent.
    llvm_build.install_distribution(extras=oop_targets)

    # llvm-jitlink-executor's CMakeLists registers an install() rule with
    # COMPONENT defaulting to "Unspecified", so it can't ride DIST
    # components. Copy by hand into bin/ so consumers find it next to
    # clang at $LLVM/bin/llvm-jitlink-executor.
    src_jitlink = build_dir / "bin" / "llvm-jitlink-executor"
    if src_jitlink.is_file():
        dst = out_dir / "llvm-project" / "bin" / "llvm-jitlink-executor"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_jitlink, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    llvm_build.smoke()

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
