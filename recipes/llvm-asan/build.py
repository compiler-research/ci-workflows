#!/usr/bin/env python3
"""Builds an asan+ubsan-instrumented Clang/LLVM install tree.

Recipe-specific bits live here (source clone, cmake flags, ninja
targets, post-install hooks). The shared install-tree publish flow
(env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS,
install-distribution, find_package smoke) lives in actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (release/{version}.x).

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

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


def _oop_targets(build_dir: Path) -> list[str]:
    """Discover orc_rt_<platform> ninja targets in the configured build.

    The target name varies per platform (orc_rt_osx, orc_rt_linux_x86_64,
    orc_rt_iossim, …). Compiler-rt is enabled solely for the OOP-JIT
    runtime that CppInterOp's clang-repl-based driver uses; LLVM_USE_SANITIZER
    propagates to every C/C++ target so the OOP runtime artifacts ship
    asan/ubsan-instrumented (matches the pre-migration behaviour). If a
    downstream consumer reports doubled asan reports, look here first.
    """
    out = subprocess.run(
        ["ninja", "-t", "targets", "all"],
        cwd=build_dir, check=False, capture_output=True, text=True,
    ).stdout
    seen = set()
    for line in out.splitlines():
        m = re.match(r"^(orc_rt[^:]*):", line)
        if m:
            seen.add(m.group(1))
    return sorted(seen)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    os.chdir(work_dir)
    if not (work_dir / "llvm-project" / ".git").is_dir():
        subprocess.run(
            ["git", "clone", "--depth=1", "-b", f"release/{version}.x",
             "https://github.com/llvm/llvm-project.git"],
            check=True,
        )

    os.chdir(work_dir / "llvm-project")
    src_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    github_env = os.environ.get("GITHUB_ENV", "")
    if github_env:
        with open(github_env, "a") as f:
            f.write(f"SRC_COMMIT={src_commit}\n")

    build_dir = work_dir / "llvm-project" / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    cmake_args = [
        "cmake", "-G", "Ninja",
        f"-DCMAKE_INSTALL_PREFIX={out_dir / 'llvm-project'}",
        '-DLLVM_ENABLE_PROJECTS=clang;compiler-rt',
        '-DLLVM_TARGETS_TO_BUILD=host;NVPTX',
        '-DCMAKE_BUILD_TYPE=Release',
        '-DLLVM_ENABLE_ASSERTIONS=ON',
        '-DLLVM_USE_SANITIZER=Address;Undefined',
        '-DCLANG_ENABLE_STATIC_ANALYZER=OFF',
        '-DCLANG_ENABLE_ARCMT=OFF',
        '-DCLANG_ENABLE_FORMAT=OFF',
        '-DCLANG_ENABLE_BOOTSTRAP=OFF',
        '-DLLVM_INCLUDE_BENCHMARKS=OFF',
        '-DLLVM_INCLUDE_EXAMPLES=OFF',
        '-DLLVM_INCLUDE_TESTS=OFF',
        '-DCOMPILER_RT_BUILD_BUILTINS=OFF',
        '-DCOMPILER_RT_BUILD_LIBFUZZER=OFF',
        '-DCOMPILER_RT_BUILD_PROFILE=OFF',
        '-DCOMPILER_RT_BUILD_MEMPROF=OFF',
        '-DCOMPILER_RT_BUILD_SANITIZERS=OFF',
        '-DCOMPILER_RT_BUILD_XRAY=OFF',
        '-DCOMPILER_RT_BUILD_GWP_ASAN=OFF',
        '-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF',
    ] + llvm_build.cmake_extra() + ["../llvm"]
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore"],
        check=True,
    )

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

    # Pass the OOP_TARGETS as extra DIST_COMPONENTS so install-distribution
    # installs them and LLVMExports.cmake stays self-consistent.
    llvm_build.install_distribution(extras=oop_targets)

    # llvm-jitlink-executor's CMakeLists registers an install() rule with
    # COMPONENT defaulting to "Unspecified", so it can't be in
    # DISTRIBUTION_COMPONENTS. Copy by hand into the install bin/ so
    # consumers find it next to clang at $LLVM/bin/llvm-jitlink-executor.
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
