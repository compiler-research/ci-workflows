#!/usr/bin/env python3
"""Builds an LLVM/Clang install tree cross-compiled for wasm32 via emsdk.

Recipe-specific bits live here (emsdk install/activate, source clone,
patch application, native-tblgen bootstrap, emcmake/emmake, source-tree
trim). The shared install-tree publish flow's helpers (env validation,
SRC_COMMIT recording) come from actions/lib/llvm_build.py; the wasm
artifact is the trimmed source+build tree, not a cmake --install tree,
so the LLVM_DISTRIBUTION_COMPONENTS / smoke() path doesn't apply.

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
import shutil
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
    "-DCLANG_ENABLE_ARCMT=OFF",
    "-DCLANG_ENABLE_BOOTSTRAP=OFF",
    # emscripten libc lacks wait4; redirect to the syscall wrapper.
    "-DCMAKE_CXX_FLAGS=-Dwait4=__syscall_wait4",
    "-DCMAKE_C_FLAGS_RELEASE=-Oz -g0 -DNDEBUG",
    "-DCMAKE_CXX_FLAGS_RELEASE=-Oz -g0 -DNDEBUG",
    "-DLLVM_ENABLE_LTO=Full",
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


def trim_source_tree(repo: Path) -> None:
    """Drop everything from llvm-project/ except build/ + native_build/
    + the trimmed llvm/ and clang/ subtrees (include + cmake only).

    Source-tree `lib/` dirs hold .cpp that's already linked into
    build/lib/*.a; nothing on the consumer include path references them.
    """
    keep_top = {"build", "llvm", "clang", "native_build"}
    for entry in repo.iterdir():
        if entry.name in keep_top:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    keep_sub = {"include", "cmake"}
    for d in ("llvm", "clang"):
        sub = repo / d
        if not sub.is_dir():
            continue
        for entry in sub.iterdir():
            if entry.name in keep_sub:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


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

    # Native tblgen bootstrap. emcmake's wasm clang can't build host
    # binaries; LLVM_NATIVE_TOOL_DIR points at this directory.
    native_build = repo / "native_build"
    native_build.mkdir(exist_ok=True)
    subprocess.run(
        ["cmake",
         "-DLLVM_ENABLE_PROJECTS=clang",
         "-DLLVM_TARGETS_TO_BUILD=host",
         "-DCMAKE_BUILD_TYPE=Release",
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

    build = repo / "build"
    build.mkdir(exist_ok=True)
    cmake_args = list(COMMON_FLAGS) + [
        f"-DLLVM_NATIVE_TOOL_DIR={native_build / 'bin'}",
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

    trim_source_tree(repo)

    # Move the trimmed tree to OUT_DIR/llvm-project for the publish step.
    dst = out_dir / "llvm-project"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(repo), str(dst))

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
