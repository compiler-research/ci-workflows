#!/usr/bin/env python3
"""Builds a vanilla Clang/LLVM install tree from an upstream LLVM ref.

Recipe-specific bits live here (source clone, cmake flags, ninja
targets, post-install hooks). The shared install-tree publish flow
(env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS,
install-distribution, find_package smoke) lives in
actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         LLVM major ('22'), or a release tag suffix
                         ('23.1.0-rc2'). See _source_ref.
  RECIPE_ARCH            optional cell arch slug for cross compilation

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built
  SRC_REF                the ref that sha was cloned from

FIXME: dedup with recipes/llvm-asan/build.py.
The compiler-rt OFF flags below and the _oop_targets() helper are
near-verbatim copies from llvm-asan/build.py. Lift the two into
actions/lib/llvm_build.py once a third LLVM-family recipe needs them
(e.g. llvm-msan), or sooner if the duplication starts drifting between
the two recipes. Note that actions/lib/**.py is in the recipe cache
key, so the lift invalidates every cell in cells.yaml -- land it with
a change that has to rebuild them anyway.
The shape ranged from "small focused helpers" to "build_llvm_release
function with kwargs that other recipes specialize"; pick at lift time.

llvm-asan and llvm-debug still hand-copy the lit utilities out of
build/bin. They can take the LLVM_INSTALL_UTILS route below instead;
each move invalidates only its own cells.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402

# LLVM test utilities a consumer's lit suite resolves under the recipe
# install prefix ($LLVM/bin/FileCheck; a few RUN lines use count/not).
_LIT_UTILS = ["FileCheck", "count", "not"]


def _source_ref(version: str) -> str:
    """Map the recipe version to a ref on llvm/llvm-project.

    A bare major ('22') tracks release/22.x, which keeps moving as
    point releases land; the key covers the version, not the commit,
    so such a cell holds whatever HEAD it warmed on. Anything else is
    an upstream tag ('23.1.0-rc2' -> llvmorg-23.1.0-rc2), immutable,
    so the cell names exactly what it holds.
    """
    if re.fullmatch(r"\d+", version):
        return f"release/{version}.x"
    return f"llvmorg-{version}"


def _record_src_ref(ref: str) -> None:
    """Publish the cloned ref as SRC_REF for the manifest step.

    build_manifest.py otherwise rebuilds source.branch from
    recipe.yaml's branch_template, which describes the bare-major form
    only.
    """
    github_env = os.environ.get("GITHUB_ENV", "")
    if not github_env:
        return
    with open(github_env, "a") as f:
        f.write(f"SRC_REF={ref}\n")


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

    # Parse + validate the LLVM major before any side effects; the
    # prefix reads the same out of '22' and out of '23.1.0-rc2'.
    # The threshold (>= 22) governs the OOP-JIT compiler-rt branch;
    # the previous form silently fell back to need_oop=False on any
    # ValueError, which produced an artifact without the OOP runtime
    # when the caller passed a non-integer version (e.g. '22.1') —
    # exactly the consumers most likely to need OOP. Parse the major
    # prefix and refuse anything that doesn't yield an integer, so the
    # misuse surfaces before any git operation rather than as a clone
    # against a ref nobody publishes.
    try:
        major = int(version.split('.')[0])
    except ValueError:
        print(
            f"::error::recipe llvm-release expects an LLVM major "
            f"(e.g. '22') or a release tag suffix (e.g. '23.1.0-rc2'); "
            f"got '{version}'",
            file=sys.stderr,
        )
        return 1
    # 32-bit Windows cells skip the OOP-JIT runtime: orc_rt's COFF
    # support is x86_64-only
    win32 = sys.platform == "win32" and \
        os.environ.get("RECIPE_ARCH") == "x86"
    need_oop = major >= 22 and not win32

    os.chdir(work_dir)
    src_ref = _source_ref(version)
    print(f"build.py: version={version}; cloning {src_ref}", flush=True)
    llvm_build.clone_shallow(
        "https://github.com/llvm/llvm-project.git",
        src_ref,
        work_dir / "llvm-project",
    )
    src_commit = llvm_build.record_src_commit(work_dir / "llvm-project")
    _record_src_ref(src_ref)

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
        llvm_build.base_cmake_args(str(out_dir / "install"))
        + [f"-DLLVM_ENABLE_PROJECTS={projects}",
           # FileCheck and friends go through add_llvm_utility(), which
           # emits an install rule and an install-<tool> target only under
           # LLVM_INSTALL_UTILS -- OFF by default, hence no test tools in
           # the artifact. With it ON they install into
           # LLVM_UTILS_INSTALL_DIR, which defaults to bin/, and become
           # ordinary LLVM_DISTRIBUTION_COMPONENTS candidates. Utilities
           # left out of the distribution get an empty export arg
           # (LLVMDistributionSupport.cmake, get_target_export_arg), so
           # this ships the ones named below and no others.
           "-DLLVM_INSTALL_UTILS=ON"]
        + llvm_build.dylib_flags()
        + compiler_rt_flags
        + llvm_build.cmake_extra()
        + ["../llvm"]
    )
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    # The lit utilities ride the main ninja line so they are on disk
    # before cleanup_intermediates() drops the objects they build from.
    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore",
         *_LIT_UTILS],
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
    # ships them and LLVMExports.cmake stays self-consistent. The lit
    # utilities and the OOP-JIT executor ride along the same way -- the
    # executor is an add_llvm_utility() too, so LLVM_INSTALL_UTILS gives
    # it a component like any other and cmake names the installed file
    # for us (llvm-jitlink-executor.exe on Windows).
    dist_extras = oop_targets + _LIT_UTILS
    if need_oop:
        dist_extras.append("llvm-jitlink-executor")
    llvm_build.install_distribution(extras=dist_extras)

    # Fail the publish rather than the consumer: an artifact without the
    # lit utilities is exactly the bug this recipe just grew a fix for.
    exe = ".exe" if sys.platform == "win32" else ""
    required = [f"bin/{t}{exe}" for t in _LIT_UTILS]
    if need_oop:
        required.append(f"bin/llvm-jitlink-executor{exe}")
    llvm_build.smoke(required_files=required)

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
