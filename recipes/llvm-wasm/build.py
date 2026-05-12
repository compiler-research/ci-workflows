#!/usr/bin/env python3
"""Builds an LLVM/Clang install tree cross-compiled for wasm32 via emsdk.

Recipe-specific bits live here (emsdk install/activate, source clone,
patch application, native-tblgen bootstrap, emcmake/emmake, two
cmake --install passes). Shared scaffolding (env validation,
SRC_COMMIT recording, LLVM_DISTRIBUTION_COMPONENTS plumbing) comes
from actions/lib/llvm_build.py.

Layout shipped under OUT_DIR/install/:
  native_build/ -- host install tree: tblgen binaries, transitive
                   LLVM libs (LLVMSupport, LLVMTableGen, ...), cmake
                   configs. Bootstraps the consumer's host
                   cppinterop-tblgen via find_package(LLVM).
  build/        -- wasm install tree: clang*/LLVM* libs built for
                   wasm32-unknown-emscripten, cmake configs, headers.
                   Consumer uses for find_package(LLVM/Clang) under
                   emcmake.

Both trees use cmake --install with relocatable cmake configs
(LLVMTargets.cmake derives prefix from ${CMAKE_CURRENT_LIST_DIR}),
so the asset extracts to any consumer workspace path correctly.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (release/{version}.x).
  EMSDK_VERSION          emsdk tag to install/activate. Defaulted from
                         recipe.yaml's `emsdk_version`.

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of llvm-project HEAD that was built
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


# Wasm cmake flags. List form is what record_cmake_args expects.
COMMON_FLAGS: list[str] = [
    "-G", "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DLLVM_HOST_TRIPLE=wasm32-unknown-emscripten",
    "-DLLVM_TARGETS_TO_BUILD=WebAssembly",
    "-DLLVM_ENABLE_PROJECTS=clang;lld",
    "-DLLVM_ENABLE_LIBEDIT=OFF",
    "-DLLVM_ENABLE_ZSTD=OFF",
    "-DLLVM_ENABLE_LIBXML2=OFF",
    "-DLLVM_ENABLE_LIBPFM=OFF",
    "-DLLVM_ENABLE_THREADS=OFF",
    "-DLLVM_INCLUDE_BENCHMARKS=OFF",
    "-DLLVM_INCLUDE_EXAMPLES=OFF",
    "-DLLVM_INCLUDE_TESTS=OFF",
    "-DLLVM_BUILD_TOOLS=OFF",
    "-DCLANG_BUILD_TOOLS=OFF",
    "-DCLANG_ENABLE_STATIC_ANALYZER=OFF",
    # CLANG_ENABLE_ARCMT was deprecated in clang 22 (ARCMigrate removed);
    # CLANG_ENABLE_OBJC_REWRITER is the supported successor.
    "-DCLANG_ENABLE_OBJC_REWRITER=OFF",
    "-DCLANG_ENABLE_BOOTSTRAP=OFF",
    # emscripten libc lacks wait4; redirect to the syscall wrapper.
    "-DCMAKE_CXX_FLAGS=-Dwait4=__syscall_wait4",
    "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG",
    "-DCMAKE_CXX_FLAGS_RELEASE=-Oz -g0 -DNDEBUG",
    "-DLLVM_ENABLE_LTO=Full",
    # cmake's default RPATH model relinks at install time. Wasm32 isn't
    # ELF, so Ninja refuses to emit the relink rule and the generate step
    # errors out on every executable/library install rule (llvm-tblgen,
    # libclang, ...) even when LLVM_BUILD_TOOLS=OFF skips the actual build.
    # No RPATH is consumed on wasm anyway; bake it at link time and skip.
    "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
]

# Targets to build for the wasm cell. Matches CppInterOp's
# Build_LLVM_WASM non-cling path. cling rows (clang/cling/lld/
# gtest_main) are not built today; see recipe.yaml.
WASM_TARGETS: list[str] = ["libclang", "clangInterpreter", "clangStaticAnalyzerCore"]


def install_emsdk(work_dir: Path, version: str) -> Path:
    """Clone emsdk into work_dir, install + activate the requested version.

    Returns the emsdk install root. Idempotent on a re-run with cache.
    """
    emsdk_dir = work_dir / "emsdk"
    if not emsdk_dir.exists():
        subprocess.run(
            ["git", "clone", "--depth=1",
             "https://github.com/emscripten-core/emsdk.git", str(emsdk_dir)],
            check=True,
        )
    # `emsdk install` / `activate` are idempotent and quick on a hit.
    emsdk = str(emsdk_dir / "emsdk")
    subprocess.run([emsdk, "install", version], check=True, cwd=emsdk_dir)
    subprocess.run([emsdk, "activate", version], check=True, cwd=emsdk_dir)
    return emsdk_dir


def apply_patches(repo: Path, version: str) -> None:
    """Apply patches/emscripten-clang{version}-*.patch in lexical order.

    No-op when the major has no matching patches (LLVM 19 today). Skip
    is a notice, not an error -- adding a new major without patches is
    a valid state.
    """
    pattern = str(SCRIPT_DIR / "patches" / f"emscripten-clang{version}-*.patch")
    patches = sorted(glob.glob(pattern))
    if not patches:
        print(f"build.py: no patches matched {pattern}; "
              "proceeding without -- valid if the major needs none.",
              file=sys.stderr)
        return
    for patch in patches:
        subprocess.run(["git", "apply", "-v", patch], check=True, cwd=repo)


def run_in_emsdk(cmd: list[str], emsdk_dir: Path, cwd: Path) -> None:
    """Run `cmd` (a shell-friendly list) under a shell that has sourced
    emsdk_env.sh. emcmake / emmake rely on EMSDK / PATH set by that
    script; subprocess starts a fresh shell each call, so source-then-run
    via bash -c is the simplest cross-cell idiom on Linux/macOS.

    shlex.quote every arg: cmake values like LLVM_ENABLE_PROJECTS=clang;lld
    contain shell metacharacters and bash would otherwise split them.
    """
    env_sh = shlex.quote(str(emsdk_dir / "emsdk_env.sh"))
    joined = " ".join(shlex.quote(c) for c in cmd)
    subprocess.run(
        ["bash", "-c", f"source {env_sh} && {joined}"],
        check=True, cwd=cwd,
    )


def _walk_built_libs(build_dir: Path) -> list[str]:
    """Return clang*/LLVM* component names for every .a in build_dir/lib/."""
    out: list[str] = []
    lib = build_dir / "lib"
    if not lib.is_dir():
        return out
    for f in sorted(lib.iterdir()):
        name = f.name
        if not name.endswith(".a"):
            continue
        base = name[3:] if name.startswith("lib") else name
        base = base[:-2]
        # libclang.a's cmake target is `libclang` (lib prefix is part of
        # the target name). Stripping it collapses to bare `clang`, which
        # cmake then resolves to the clang-driver executable component
        # that CLANG_BUILD_TOOLS=OFF skips -- install-clang doesn't exist
        # and the DIST step errors with "doesn't have an install target".
        if base == "clang":
            base = "libclang"
        # lld* covers liblldCommon.a / liblldWasm.a / liblldELF.a; LLVM 22
        # added them as transitive deps of clangInterpreter, so omitting
        # them from DIST trips ClangTargets export with "requires target
        # lldWasm which is not in any export set".
        if (base.startswith("clang") or base.startswith("LLVM")
                or base.startswith("lld") or base == "libclang"):
            out.append(base)
    return out


def _native_dist_components(build_dir: Path) -> list[str]:
    """Host-stage DIST: tblgens + cmake-exports + llvm-headers + every
    built LLVM*/clang* lib. Drops `clangInterpreter` and `clang` (not
    built on the host bootstrap) and `llvm-config` (the consumer's
    find_package path doesn't need it).
    """
    return [
        "llvm-tblgen", "clang-tblgen",
        "cmake-exports", "llvm-headers",
    ] + _walk_built_libs(build_dir)


def _wasm_dist_components(build_dir: Path) -> list[str]:
    """Wasm-stage DIST: clang headers + cmake-exports + libs we built.
    LLVM_BUILD_TOOLS=OFF / CLANG_BUILD_TOOLS=OFF skip host binaries
    (llvm-tblgen, clang-tblgen, llvm-config, clang) so they're absent
    from the dist set -- including them would fail
    `ninja install-X` at file-not-found.
    """
    return [
        "clang-headers", "clang-cmake-exports", "clang-resource-headers",
        "clangInterpreter",
        # lld-cmake-exports installs lib/cmake/lld/LLDConfig.cmake; without
        # it, consumers passing `-DLLD_DIR=$LLVM_BUILD_DIR/lib/cmake/lld`
        # hit `Could not find a package configuration file provided by "LLD"`.
        # lld-headers ships include/lld/ for `#include "lld/..."` from the
        # consumer side.
        "lld-cmake-exports", "lld-headers",
        "cmake-exports", "llvm-headers",
    ] + _walk_built_libs(build_dir)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]
    # Default mirrors recipe.yaml's emsdk_version; publish-recipe could
    # also set it from a cell-level override later if needed.
    emsdk_version = os.environ.get("EMSDK_VERSION", "4.0.9")

    os.chdir(work_dir)
    emsdk_dir = install_emsdk(work_dir, emsdk_version)

    llvm_build.clone_shallow(
        "https://github.com/llvm/llvm-project.git",
        f"release/{version}.x",
        work_dir / "llvm-project",
    )
    src_commit = llvm_build.record_src_commit(work_dir / "llvm-project")

    repo = work_dir / "llvm-project"
    apply_patches(repo, version)

    install_root = out_dir / "install"
    native_install = install_root / "native_build"
    wasm_install = install_root / "build"

    # Native tblgen bootstrap. emcmake's wasm clang can't build host
    # binaries; LLVM_NATIVE_TOOL_DIR points at this directory.
    native_build = repo / "native_build"
    native_build.mkdir(exist_ok=True)
    subprocess.run(
        ["cmake",
         "-DLLVM_ENABLE_PROJECTS=clang",
         "-DLLVM_TARGETS_TO_BUILD=host",
         "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_INSTALL_PREFIX={native_install}",
         "-G", "Ninja",
         "../llvm"],
        check=True, cwd=native_build,
    )
    subprocess.run(
        ["cmake", "--build", ".",
         "--target", "llvm-tblgen", "clang-tblgen",
         "--parallel", ncpus],
        check=True, cwd=native_build,
    )
    # Install host tree with a tailored DIST: skip the consumer-tier
    # components (clang, clangInterpreter) the bootstrap doesn't need.
    # `cwd` switch is required: run_install_distribution operates on `.`.
    os.chdir(native_build)
    llvm_build.run_install_distribution(
        ";".join(_native_dist_components(native_build))
    )
    os.chdir(work_dir)

    build = repo / "build"
    build.mkdir(exist_ok=True)
    # LLVM_TABLEGEN / CLANG_TABLEGEN are explicit binary paths; setting
    # them bypasses the nested `build/NATIVE/` sub-cmake that
    # LLVM_NATIVE_TOOL_DIR alone doesn't always suppress. The nested
    # configure was failing on its own RPATH-at-install rules (mirrors
    # the issue CMAKE_BUILD_WITH_INSTALL_RPATH=ON fixes for the outer
    # build). CROSS_TOOLCHAIN_FLAGS_NATIVE forwards the same RPATH flag
    # as a defensive measure if any other nested configure still fires.
    cmake_args = list(COMMON_FLAGS) + [
        f"-DCMAKE_INSTALL_PREFIX={wasm_install}",
        f"-DLLVM_TABLEGEN={native_install / 'bin' / 'llvm-tblgen'}",
        f"-DCLANG_TABLEGEN={native_install / 'bin' / 'clang-tblgen'}",
        "-DCROSS_TOOLCHAIN_FLAGS_NATIVE=-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
        "../llvm",
    ]
    llvm_build.record_cmake_args(["emcmake", "cmake", *cmake_args])
    run_in_emsdk(["emcmake", "cmake", *cmake_args], emsdk_dir, build)

    # Smoke-test hook: emmake the demangle library only and exit. emcmake
    # configure is the wasm-side regression surface (toolchain file, patch
    # apply); running just one wasm target keeps the dry-run minutes
    # instead of hours.
    if os.environ.get("RECIPE_QUICK_CHECK") == "1":
        run_in_emsdk(
            ["emmake", "ninja", "-j", ncpus, "LLVMDemangle"],
            emsdk_dir, build,
        )
        print("build.py: RECIPE_QUICK_CHECK=1 -> built LLVMDemangle, exiting.",
              flush=True)
        return 0

    # EMCC_CFLAGS=-fwasm-exceptions: wasm exception ABI for the targets
    # CppInterOp links against. Build_LLVM_WASM passes this same flag.
    env_sh = shlex.quote(str(emsdk_dir / "emsdk_env.sh"))
    targets = " ".join(shlex.quote(t) for t in WASM_TARGETS)
    subprocess.run(
        ["bash", "-c",
         f"source {env_sh} && "
         f"EMCC_CFLAGS=-fwasm-exceptions "
         f"emmake ninja -j {shlex.quote(ncpus)} {targets}"],
        check=True, cwd=build,
    )

    # Install wasm tree. install rules are pure file/cmake-config
    # operations -- no emcc invocation needed beyond what WASM_TARGETS
    # already built, so plain ninja (not emmake) is sufficient.
    os.chdir(build)
    llvm_build.run_install_distribution(";".join(_wasm_dist_components(build)))

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
