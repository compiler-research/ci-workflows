#!/usr/bin/env bash
# Builds an asan+ubsan-instrumented Clang/LLVM install tree.
#
# Recipe-specific bits live here (source clone, cmake flags, ninja
# targets, post-install hooks). The shared install-tree publish flow
# (env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS, install-
# distribution, find_package smoke) lives in actions/lib/llvm-build.sh.
#
# Inputs (env): see actions/lib/llvm-build.sh header.
#   RECIPE_VERSION         major LLVM version, e.g. 22; substitutes
#                          into release/{version}.x.
#
# Outputs (env, written to GITHUB_ENV when present):
#   SRC_COMMIT             sha of llvm-project HEAD that was built
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../actions/lib/llvm-build.sh
source "$SCRIPT_DIR/../../actions/lib/llvm-build.sh"

llvm_build::setup_env

cd "$WORK_DIR"
if [[ ! -d llvm-project/.git ]]; then
  git clone --depth=1 -b "release/${RECIPE_VERSION}.x" \
    https://github.com/llvm/llvm-project.git
fi

cd llvm-project
SRC_COMMIT="$(git rev-parse HEAD)"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "SRC_COMMIT=${SRC_COMMIT}" >> "$GITHUB_ENV"
fi

mkdir -p build
cd build

# mapfile would be cleaner here but isn't available on bash 3.2
# (macOS /bin/bash). while-read is portable.
cmake_extra=()
while IFS= read -r line; do cmake_extra+=("$line"); done \
  < <(llvm_build::cmake_extra)

cmake -G Ninja \
  -DCMAKE_INSTALL_PREFIX="$OUT_DIR/llvm-project" \
  -DLLVM_ENABLE_PROJECTS="clang;compiler-rt" \
  -DLLVM_TARGETS_TO_BUILD="host;NVPTX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_USE_SANITIZER="Address;Undefined" \
  -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
  -DCLANG_ENABLE_ARCMT=OFF \
  -DCLANG_ENABLE_FORMAT=OFF \
  -DCLANG_ENABLE_BOOTSTRAP=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_TESTS=OFF \
  -DCOMPILER_RT_BUILD_BUILTINS=OFF \
  -DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
  -DCOMPILER_RT_BUILD_PROFILE=OFF \
  -DCOMPILER_RT_BUILD_MEMPROF=OFF \
  -DCOMPILER_RT_BUILD_SANITIZERS=OFF \
  -DCOMPILER_RT_BUILD_XRAY=OFF \
  -DCOMPILER_RT_BUILD_GWP_ASAN=OFF \
  -DCOMPILER_RT_BUILD_CTX_PROFILE=OFF \
  ${cmake_extra[@]+"${cmake_extra[@]}"} \
  ../llvm

llvm_build::quick_check_or_continue

ninja -j "${NCPUS}" clang clangInterpreter clangStaticAnalyzerCore

# compiler-rt is enabled solely for the OOP-JIT runtime that CppInterOp's
# clang-repl-based driver uses. The orc_rt target name varies per
# platform (orc_rt_osx, orc_rt_linux_x86_64, …); enumerate via
# `ninja -t targets` and build whatever matches, plus the executor.
#
# Why: LLVM_USE_SANITIZER=Address;Undefined propagates to *every* C/C++
# target, including orc_rt and llvm-jitlink-executor. The OOP runtime
# artifacts therefore ship with asan/ubsan instrumentation baked in.
# A future change that lets the OOP runtime call into untrusted JIT'd
# code could expose double-instrumentation surprises; if a downstream
# consumer reports doubled asan reports, this is the place to start.
OOP_TARGETS=$(ninja -t targets all 2>/dev/null | \
  awk -F: '/^orc_rt[^:]*:/{print $1}' | sort -u | tr '\n' ' ')
if [[ -n "${OOP_TARGETS}" ]]; then
  ninja -j "${NCPUS}" llvm-jitlink-executor ${OOP_TARGETS}
else
  echo "build.sh: no orc_rt targets matched; OOP-JIT runtime won't be in the artifact." >&2
fi

llvm_build::cleanup_intermediates

# Pass the OOP_TARGETS as extra DIST_COMPONENTS so install-distribution
# installs them and LLVMExports.cmake stays self-consistent.
# shellcheck disable=SC2086  # OOP_TARGETS is a space-separated list, splat intentionally
llvm_build::install_distribution ${OOP_TARGETS}

# llvm-jitlink-executor's CMakeLists registers an install() rule with
# COMPONENT defaulting to "Unspecified", so it can't be in
# DISTRIBUTION_COMPONENTS. Copy by hand into the install bin/ so
# consumers find it next to clang at $LLVM/bin/llvm-jitlink-executor.
if [[ -x bin/llvm-jitlink-executor ]]; then
  install -m 0755 bin/llvm-jitlink-executor "$OUT_DIR/llvm-project/bin/"
fi

llvm_build::smoke

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
