#!/usr/bin/env python3
"""PR-time dry-run recipe.

Goes through every shared step a real recipe uses (helper functions
for env, cmake_extra, cleanup_intermediates, run_install_distribution;
cache_pack via the publish-recipe action) but builds only LLVMDemangle
so a hosted runner finishes in ~3-5 min instead of ~30. The verify.yml
publish-dryrun matrix invokes this recipe via the real publish-recipe
action with cache-base: file:// so no upload happens — but
tar+zstd+manifest+cache_upload all run against a real install tree.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (matches release/{version}.x).

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Recipe build.py runs from $WORK_DIR; resolve our location relative
# to the script file so we can find the helper.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


def main() -> int:
    llvm_build.setup_env()

    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

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

    # Minimal LLVM-only configure: no clang, no compiler-rt, just enough
    # for LLVMDemangle to build. host targets only — host;NVPTX would
    # pull in extra deps we don't need here. Deliberately not routed
    # through base_cmake_args: the dry-run is intentionally narrower
    # than real recipes (no LLVM_ENABLE_ASSERTIONS, no CLANG_*=OFF)
    # since it never builds clang.
    cmake_args = [
        "cmake", "-G", "Ninja",
        f"-DCMAKE_INSTALL_PREFIX={out_dir / 'llvm-project'}",
        '-DLLVM_TARGETS_TO_BUILD=host',
        '-DCMAKE_BUILD_TYPE=Release',
        '-DLLVM_INCLUDE_BENCHMARKS=OFF',
        '-DLLVM_INCLUDE_EXAMPLES=OFF',
        '-DLLVM_INCLUDE_TESTS=OFF',
    ] + llvm_build.cmake_extra() + ["../llvm"]
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    subprocess.run(["ninja", "-j", ncpus, "LLVMDemangle"], check=True)

    llvm_build.cleanup_intermediates()

    # Real recipes call install_distribution, which assembles a clang-
    # centric umbrella list. This recipe ships only LLVMDemangle, so
    # call run_install_distribution directly with a minimal scope.
    #
    # Both components are strictly install-only on a configured tree:
    #   LLVMDemangle    Already built by the recipe's explicit ninja
    #                   step above; install-LLVMDemangle just installs.
    #   cmake-exports   Installs the cmake module files (LLVMConfig
    #                   .cmake et al.) at configure time with no
    #                   library deps. publish-dryrun's verify step
    #                   asserts LLVMConfig.cmake's presence.
    #
    # `llvm-headers` is deliberately excluded: install-llvm-headers
    # depends on the llvm-headers phony target, which depends on
    # intrinsics_gen, which is produced by llvm-tblgen, which links
    # against libLLVMSupport.a. So `ninja install-llvm-headers` builds
    # all ~239 LLVMSupport sources plus llvm-tblgen before installing
    # any header file -- ~5-10 min on a hosted runner, blowing through
    # the dry-run's fast-feedback budget.
    #
    # The "build before install" contract that run_install_distribution
    # enforces is pinned at unit level by
    # test_llvm_build.RunInstallDistributionTests, so dropping
    # llvm-headers here doesn't lose regression coverage.
    llvm_build.run_install_distribution(
        "LLVMDemangle;cmake-exports"
    )

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
