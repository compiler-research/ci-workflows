#!/usr/bin/env python3
"""Builds an msan-instrumented Clang/LLVM install tree.

Two-stage bootstrap on top of a prebuilt bootstrap clang (the
llvm-release cell, fetched by publish-recipe.yml's fetch_bootstrap
step before this script runs and exposed via BOOTSTRAP_CLANG_BIN):

  Stage 1: msan-instrumented libc++/libcxxabi/libunwind from
           llvm-project/runtimes, compiled with the bootstrap clang.
  Stage 2: LLVM/Clang/compiler-rt with -fsanitize=memory, compiled
           with the bootstrap clang and linked against stage 1's
           libc++.

Why a separate bootstrap clang: building libc++ from llvm-project
release/{N}.x requires a Clang at least N (libc++ headers use
builtins like __builtin_ctzg added in Clang 19+), and apt-noble's
clang-18 is too old to compile libc++ from llvm-22. The published
llvm-release cell IS the matching clang, so consume it as the
bootstrap rather than rebuild scaffolding from source.

Why msan needs an instrumented libc++: MSan tracks per-byte
initialisation propagation. Every write the resulting LLVM later
reads must be either instrumented or covered by a compiler-rt
interceptor; an uninstrumented system libc++ / libstdc++ trips
false positives on the first allocator-fed buffer the resident
clang reads. libstdc++ is not a supported MSan stdlib upstream;
the bundled libc++ is the only correct configuration.

Reference: llvm-zorg's buildbot_bootstrap_msan.sh + buildbot_functions.sh
build_stage2() msan path.

Inputs (env): see actions/lib/llvm_build.py docstring.
  RECIPE_VERSION         major LLVM version (release/{version}.x).
  MSAN_FLAVOR            'Memory' (default) or 'MemoryWithOrigins' for
                         origin tracking. Origins double the runtime
                         memory cost but report where uninitialised
                         values came from.

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


# Off by default to keep the published cell smaller and the build
# tractable; flip via env when origin reports are wanted.
MSAN_FLAVOR = os.environ.get("MSAN_FLAVOR", "Memory")


def _oop_targets(build_dir: Path) -> list[str]:
    """orc_rt_<platform> ninja targets present in `build_dir`.

    Same logic as llvm-asan's build.py — kept duplicated rather than
    pulled into llvm_build.py because the two recipes still differ in
    enough other ways that the right shared abstraction isn't obvious
    yet (see the 'figure out the abstraction' discussion in the PR
    that introduces this recipe).
    """
    out = subprocess.run(
        ["ninja", "-t", "targets", "all"],
        cwd=build_dir, check=False, capture_output=True, text=True,
    ).stdout
    seen: set[str] = set()
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


def _build_msan_runtime_into(src_dir: Path, build_dir: Path,
                             bootstrap_bin: Path, ncpus: str) -> None:
    """Build compiler-rt's MSan runtime with the bootstrap clang and
    install it into the bootstrap clang's resource-dir.

    Why: the published llvm-release cell we use as the bootstrap was
    built with COMPILER_RT_BUILD_SANITIZERS=OFF, so the bootstrap
    clang has no libclang_rt.msan-x86_64.a in its lib/clang/<N>/lib/
    tree -- the moment stage 1's cmake compiler-test tries to link a
    `-fsanitize=memory` program, ld errors with "cannot find
    libclang_rt.msan.a". Build it ourselves into the same prefix the
    bootstrap clang installs to, which is the local extracted dir
    (writes here don't pollute the published cache cell).

    Cheap with ccache enabled: compiler-rt's source is small and the
    publish-recipe action exports CMAKE_C_COMPILER_LAUNCHER=ccache
    which cmake honors; warm runs are seconds. Build as a separate
    step (rather than rolling compiler-rt into stage 2) so the
    bootstrap clang has the runtime *before* stage 1 needs to link
    libc++.
    """
    # bootstrap_bin is `<extract>/llvm-project/bin`. The bootstrap
    # clang searches `<extract>/llvm-project/lib/clang/<N>/lib/<triple>/`
    # for runtime archives, so install directly into the resource-dir
    # (with PER_TARGET_RUNTIME_DIR on, files land at
    # `<prefix>/lib/<triple>/libclang_rt.*`). Probe the bootstrap's
    # existing resource-dir for the right N rather than hardcoding.
    resource_dirs = sorted(
        (bootstrap_bin.parent / "lib" / "clang").glob("[0-9]*"),
        key=lambda p: int(p.name),
    )
    if not resource_dirs:
        print("build.py: bootstrap clang has no lib/clang/<N>/ "
              "resource-dir; aborting", file=sys.stderr)
        sys.exit(1)
    install_prefix = resource_dirs[-1]

    build_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["cmake", "-G", "Ninja",
         "-S", str(src_dir / "runtimes"),
         "-B", str(build_dir),
         f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
         "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_C_COMPILER={bootstrap_bin}/clang",
         f"-DCMAKE_CXX_COMPILER={bootstrap_bin}/clang++",
         "-DLLVM_ENABLE_RUNTIMES=compiler-rt",
         # PER_TARGET_RUNTIME_DIR=ON makes runtimes install at
         # <prefix>/lib/<triple>/ rather than <prefix>/lib/linux/;
         # paired with prefix=<resource-dir>, files land exactly
         # where the bootstrap clang searches.
         "-DLLVM_ENABLE_PER_TARGET_RUNTIME_DIR=ON",
         "-DCOMPILER_RT_BUILD_BUILTINS=OFF",
         "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF",
         "-DCOMPILER_RT_BUILD_PROFILE=OFF",
         "-DCOMPILER_RT_BUILD_MEMPROF=OFF",
         "-DCOMPILER_RT_BUILD_SANITIZERS=ON",
         "-DCOMPILER_RT_BUILD_XRAY=OFF",
         "-DCOMPILER_RT_BUILD_GWP_ASAN=OFF",
         "-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF"],
        check=True,
    )
    # `install-compiler-rt` ships the libraries; `install-compiler-rt-
    # headers` ships sanitizer/{msan,asan,...}_interface.h which
    # libc++ #includes from libc/src/__support/macros/sanitizer.h
    # when LLVM_USE_SANITIZER=Memory. Skip the headers and stage 1
    # libc++ fails halfway through with "msan_interface.h not found".
    subprocess.run(
        ["ninja", "-C", str(build_dir), "-j", ncpus,
         "install-compiler-rt", "install-compiler-rt-headers"],
        check=True,
    )


def _build_stage1_libcxx(src_dir: Path, build_dir: Path,
                         install_dir: Path, bootstrap_bin: Path,
                         fsanitize: str, ncpus: str) -> Path:
    """Build + install an msan-instrumented libc++/libcxxabi/libunwind.

    Compiled with the bootstrap clang (release-major-matching) so
    libc++ headers that use new-Clang builtins compile cleanly.
    Returns the runtime directory containing libc++.so so stage 2 can
    point its rpath/-L there. Mirrors the cmake invocation in
    llvm-zorg's build_stage2() libcxx branch (msan flavor).
    """
    build_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["cmake", "-G", "Ninja",
         "-S", str(src_dir / "runtimes"),
         "-B", str(build_dir),
         f"-DCMAKE_INSTALL_PREFIX={install_dir}",
         "-DCMAKE_BUILD_TYPE=Release",
         f"-DCMAKE_C_COMPILER={bootstrap_bin}/clang",
         f"-DCMAKE_CXX_COMPILER={bootstrap_bin}/clang++",
         "-DLLVM_ENABLE_RUNTIMES=libcxx;libcxxabi;libunwind",
         f"-DLLVM_USE_SANITIZER={MSAN_FLAVOR}",
         f"-DCMAKE_C_FLAGS={fsanitize}",
         f"-DCMAKE_CXX_FLAGS={fsanitize}",
         # libcxxabi-uses-llvm-unwinder OFF matches zorg; avoids a
         # circular runtime-build dependency we don't need here.
         "-DLIBCXXABI_USE_LLVM_UNWINDER=OFF",
         "-DLIBCXX_INCLUDE_BENCHMARKS=OFF",
         "-DLIBCXX_INCLUDE_TESTS=OFF"],
        check=True,
    )
    subprocess.run(
        ["ninja", "-C", str(build_dir), "-j", ncpus, "install"], check=True,
    )

    libcxx_so = next(install_dir.rglob("libc++.so*"), None)
    if libcxx_so is None:
        print("build.py: stage 1 produced no libc++; aborting",
              file=sys.stderr)
        sys.exit(1)
    return libcxx_so.parent


def _emit_toolchain_file(install_prefix: Path) -> None:
    """Write `<install>/etc/cmake-toolchain.cmake` -- the consumer-side
    msan link contract in one cmake fragment. setup-llvm exports it as
    CMAKE_TOOLCHAIN_FILE generically, so consumer matrix rows stay a
    one-line `flavor: msan`. ${CMAKE_CURRENT_LIST_DIR}/.. resolves the
    install root, so the same file works at OUT_DIR (recipe build) and
    $GITHUB_WORKSPACE/llvm-project (extracted cell).
    """
    etc_dir = install_prefix / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)
    (etc_dir / "cmake-toolchain.cmake").write_text(
        # Non-f-string: cmake's ${...} substitutions must reach the
        # output verbatim.
        "# Generated by recipes/llvm-msan/build.py. Rebuild the recipe\n"
        "# to regenerate.\n"
        "\n"
        "get_filename_component(_LLVM_MSAN_PREFIX\n"
        '    "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)\n'
        "\n"
        "# Non-CACHE so toolchain assignments win over env CC/CXX.\n"
        'set(CMAKE_C_COMPILER   "${_LLVM_MSAN_PREFIX}/bin/clang")\n'
        'set(CMAKE_CXX_COMPILER "${_LLVM_MSAN_PREFIX}/bin/clang++")\n'
        "\n"
        "# _INIT seeds CMAKE_*_FLAGS before HandleLLVMOptions runs so\n"
        "# consumer code, libc++ headers and LLVM see one contract\n"
        "# from configure-step zero. -L is required at link time:\n"
        "# clang's -stdlib=libc++ searches the resource-dir + sysroot,\n"
        "# not <install>/lib. -fsanitize=memory at link time too --\n"
        "# libc++.so.1 is instrumented but lacks libclang_rt.msan as\n"
        "# DT_NEEDED.\n"
        'set(CMAKE_C_FLAGS_INIT   "-fsanitize=memory -stdlib=libc++")\n'
        'set(CMAKE_CXX_FLAGS_INIT "-fsanitize=memory -stdlib=libc++")\n'
        "set(_LLVM_MSAN_LDFLAGS\n"
        '    "-fsanitize=memory -stdlib=libc++ -L${_LLVM_MSAN_PREFIX}/lib -Wl,-rpath,${_LLVM_MSAN_PREFIX}/lib")\n'
        'set(CMAKE_EXE_LINKER_FLAGS_INIT    "${_LLVM_MSAN_LDFLAGS}")\n'
        'set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_LLVM_MSAN_LDFLAGS}")\n'
        'set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_LLVM_MSAN_LDFLAGS}")\n'
        "\n"
        "# HandleLLVMOptions reads these. LLVM_USE_SANITIZER=Memory\n"
        "# drops -Wl,-z,defs (otherwise libc++.so.1's unresolved\n"
        "# __msan_init trips link errors). LLVM_COMPILER_CHECKED\n"
        "# skips the LLVM_LIBSTDCXX_MIN check, which misfires under\n"
        "# libc++ even though the binary never links libstdc++.\n"
        'set(LLVM_USE_SANITIZER    "Memory" CACHE STRING "")\n'
        'set(LLVM_ENABLE_LIBCXX    ON       CACHE BOOL   "")\n'
        'set(LLVM_COMPILER_CHECKED ON       CACHE BOOL   "")\n'
    )
    print(f"build.py: wrote {etc_dir / 'cmake-toolchain.cmake'}",
          file=sys.stderr, flush=True)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    bootstrap_bin_str = os.environ.get("BOOTSTRAP_CLANG_BIN", "")
    if not bootstrap_bin_str:
        print(
            "build.py: BOOTSTRAP_CLANG_BIN is not set. The msan recipe "
            "depends on the llvm-release cell as a bootstrap clang -- "
            "publish-recipe.yml's fetch_bootstrap step is supposed to "
            "download it and export this env var. If you're running "
            "build.py outside publish-recipe (manual repro / local "
            "dev), point BOOTSTRAP_CLANG_BIN at a clang>=N install's "
            "bin/ directory where N matches RECIPE_VERSION.",
            file=sys.stderr,
        )
        return 1
    bootstrap_bin = Path(bootstrap_bin_str)
    if not (bootstrap_bin / "clang").is_file():
        print(f"build.py: BOOTSTRAP_CLANG_BIN={bootstrap_bin} has no "
              f"clang binary; aborting", file=sys.stderr)
        return 1

    src_dir = work_dir / "llvm-project"
    install_prefix = out_dir / "llvm-project"
    libcxx_install = work_dir / "libcxx_msan"

    os.chdir(work_dir)
    llvm_build.clone_shallow(
        "https://github.com/llvm/llvm-project.git",
        f"release/{version}.x",
        src_dir,
    )
    src_commit = llvm_build.record_src_commit(src_dir)

    fsanitize = "-fsanitize=memory"
    if MSAN_FLAVOR == "MemoryWithOrigins":
        fsanitize += " -fsanitize-memory-track-origins"

    # ----- Stage 0.5: graft libclang_rt.msan into the bootstrap clang -----
    # The llvm-release cell ships with COMPILER_RT_BUILD_SANITIZERS=OFF,
    # so the bootstrap clang has no MSan runtime archive. Build it now
    # using the bootstrap clang itself; ccache makes warm runs cheap.
    print("build.py: stage 0.5 -- building MSan compiler-rt runtime "
          "with bootstrap clang", flush=True)
    _build_msan_runtime_into(
        src_dir=src_dir,
        build_dir=src_dir / "build_compiler_rt_bootstrap",
        bootstrap_bin=bootstrap_bin,
        ncpus=ncpus,
    )

    # ----- Stage 1: msan-instrumented libc++ -----
    print(f"build.py: stage 1 -- building msan-instrumented libc++ "
          f"with bootstrap clang at {bootstrap_bin} "
          f"(MSAN_FLAVOR={MSAN_FLAVOR})", flush=True)
    libcxx_runtime = _build_stage1_libcxx(
        src_dir=src_dir,
        build_dir=src_dir / "build_libcxx_msan",
        install_dir=libcxx_install,
        bootstrap_bin=bootstrap_bin,
        fsanitize=fsanitize,
        ncpus=ncpus,
    )

    # ----- Stage 2: msan-instrumented LLVM/Clang -----
    print("build.py: stage 2 -- building msan-instrumented LLVM/Clang",
          flush=True)
    build_dir = src_dir / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    # libc++ as the resident stdlib: -nostdinc++ wipes the system
    # libstdc++ headers, -isystem points at stage 1's libc++ headers,
    # rpath baked at link time so the bundled runtime is found at
    # install location without LD_LIBRARY_PATH.
    sanitizer_cflags = (
        f"-nostdinc++ "
        f"-isystem {libcxx_install}/include "
        f"-isystem {libcxx_install}/include/c++/v1 "
        f"{fsanitize}"
    )
    # Two rpath entries: libcxx_runtime so binaries built and run
    # during stage 2 (e.g. llvm-min-tblgen invoked by tablegen rules)
    # find libc++.so.1 immediately, install_prefix/lib so they also
    # find it after the cell is extracted at the install location.
    # `-L` only affects link-time search; rpath drives runtime lookup.
    # `-stdlib=libc++` here (link-side only) instead of via
    # LLVM_ENABLE_LIBCXX=ON, which would add it to CXX_FLAGS and
    # spam a "-stdlib=libc++ argument unused during compilation"
    # warning per TU because -nostdinc++ already wipes the auto-
    # header path.
    sanitizer_ldflags = (
        f"-stdlib=libc++ "
        f"-Wl,-rpath,{libcxx_runtime} "
        f"-Wl,-rpath,{install_prefix}/lib "
        f"-L{libcxx_runtime}"
    )

    cmake_args = (
        llvm_build.base_cmake_args(str(install_prefix))
        + [
            "-DLLVM_ENABLE_PROJECTS=clang;compiler-rt",
            f"-DLLVM_USE_SANITIZER={MSAN_FLAVOR}",
            f"-DCMAKE_C_FLAGS={sanitizer_cflags}",
            f"-DCMAKE_CXX_FLAGS={sanitizer_cflags}",
            f"-DCMAKE_EXE_LINKER_FLAGS={sanitizer_ldflags}",
            f"-DCMAKE_SHARED_LINKER_FLAGS={sanitizer_ldflags}",
            f"-DCMAKE_C_COMPILER={bootstrap_bin}/clang",
            f"-DCMAKE_CXX_COMPILER={bootstrap_bin}/clang++",
            # compiler-rt is enabled solely for orc_rt_<platform>;
            # everything else under compiler-rt stays OFF (matches
            # llvm-asan).
            "-DCOMPILER_RT_BUILD_BUILTINS=OFF",
            "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF",
            "-DCOMPILER_RT_BUILD_PROFILE=OFF",
            "-DCOMPILER_RT_BUILD_MEMPROF=OFF",
            "-DCOMPILER_RT_BUILD_SANITIZERS=OFF",
            "-DCOMPILER_RT_BUILD_XRAY=OFF",
            "-DCOMPILER_RT_BUILD_GWP_ASAN=OFF",
            "-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF",
        ]
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
    llvm_build.install_distribution(extras=oop_targets)

    src_jitlink = build_dir / "bin" / "llvm-jitlink-executor"
    if src_jitlink.is_file():
        dst = install_prefix / "bin" / "llvm-jitlink-executor"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_jitlink, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Bundle the stage 1 libc++ runtime + headers alongside the LLVM
    # install so consumers don't have to ship them separately. The
    # rpath baked into the stage 2 binaries points at $install/lib, so
    # the resident clang finds the instrumented libc++ at runtime
    # without LD_LIBRARY_PATH games. Headers go under $install/include/
    # c++/v1/ so `-stdlib=libc++` against the cell's clang resolves
    # `<type_traits>` etc. Consumers compiling code against this LLVM
    # under -fsanitize=memory must pass -fsanitize=memory on every
    # link line too -- libc++.so.1 is msan-instrumented but does not
    # carry the msan compiler-rt runtime as DT_NEEDED, so an unadorned
    # consumer link trips `undefined reference to __msan_*`.
    print("build.py: bundling msan-instrumented libc++ runtime + "
          "headers into install tree", flush=True)
    dst_lib = install_prefix / "lib"
    dst_lib.mkdir(parents=True, exist_ok=True)
    for f in libcxx_install.glob("lib/libc++*"):
        shutil.copy2(f, dst_lib / f.name, follow_symlinks=False)
    for f in libcxx_install.glob("lib/libunwind*"):
        shutil.copy2(f, dst_lib / f.name, follow_symlinks=False)
    src_inc = libcxx_install / "include" / "c++"
    dst_inc = install_prefix / "include" / "c++"
    if src_inc.is_dir():
        if dst_inc.exists():
            shutil.rmtree(dst_inc)
        shutil.copytree(src_inc, dst_inc, symlinks=True)

    # Stage 2 was built with COMPILER_RT_BUILD_SANITIZERS=OFF so the
    # cell as installed lacks libclang_rt.msan-<arch>.a in its
    # resource-dir; consumers using the cell's clang to link any code
    # with -fsanitize=memory get "cannot find libclang_rt.msan.a".
    # Reuse the stage 0.5 helper -- it builds compiler-rt's sanitizer
    # runtimes and installs them under the bootstrap_bin's resource-
    # dir, which is exactly what we want here for the published install.
    # Second invocation is ccache-warm (same source, same flags) and
    # adds < 1 minute to the cold-build.
    print("build.py: stage 2.5 -- grafting MSan compiler-rt runtime "
          "into install tree", flush=True)
    _build_msan_runtime_into(
        src_dir=src_dir,
        build_dir=src_dir / "build_compiler_rt_install",
        bootstrap_bin=install_prefix / "bin",
        ncpus=ncpus,
    )

    _emit_toolchain_file(install_prefix)

    llvm_build.smoke()

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
