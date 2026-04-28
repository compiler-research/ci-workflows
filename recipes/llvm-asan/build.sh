#!/usr/bin/env bash
# Builds an asan+ubsan-instrumented Clang/LLVM tree.
#
# Inputs (env):
#   RECIPE_VERSION         major LLVM version, e.g. 22
#   WORK_DIR               scratch directory (clone + build live here)
#   OUT_DIR                final tree is rsync'd here for tar/upload
#   NCPUS                  parallelism (default: nproc)
#   CMAKE_C_COMPILER_LAUNCHER, CMAKE_CXX_COMPILER_LAUNCHER
#                          optional, e.g. "ccache" — passed through to cmake
#
# Outputs (env, written to GITHUB_ENV when present):
#   SRC_COMMIT             sha of llvm-project HEAD that was built
set -euo pipefail

: "${RECIPE_VERSION:?RECIPE_VERSION must be set}"
: "${WORK_DIR:?WORK_DIR must be set}"
: "${OUT_DIR:?OUT_DIR must be set}"
NCPUS="${NCPUS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.logicalcpu 2>/dev/null || echo 4)}"

mkdir -p "$WORK_DIR" "$OUT_DIR"
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

cmake_extra=()
[[ -n "${CMAKE_C_COMPILER_LAUNCHER:-}" ]] && \
  cmake_extra+=( -DCMAKE_C_COMPILER_LAUNCHER="${CMAKE_C_COMPILER_LAUNCHER}" )
[[ -n "${CMAKE_CXX_COMPILER_LAUNCHER:-}" ]] && \
  cmake_extra+=( -DCMAKE_CXX_COMPILER_LAUNCHER="${CMAKE_CXX_COMPILER_LAUNCHER}" )
[[ -n "${CMAKE_C_COMPILER:-}" ]] && \
  cmake_extra+=( -DCMAKE_C_COMPILER="${CMAKE_C_COMPILER}" )
[[ -n "${CMAKE_CXX_COMPILER:-}" ]] && \
  cmake_extra+=( -DCMAKE_CXX_COMPILER="${CMAKE_CXX_COMPILER}" )

cmake -G Ninja \
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
  "${cmake_extra[@]}" \
  ../llvm

# RECIPE_QUICK_CHECK=1 builds only the smallest LLVM library (LLVMDemangle)
# and exits successfully. Used by verify.yml as a PR-time smoke check
# that catches host-toolchain mismatches in ~3 min — the actual mode
# this guards against was a real publish failure post-merge where the
# runner picked up gcc by default and rejected Clang-only UBSan flags.
# Building LLVMDemangle exercises the same compiler invocation as the
# first ~30 source files of the full build without paying the rest of
# the ~30 min.
if [[ "${RECIPE_QUICK_CHECK:-0}" == "1" ]]; then
  ninja -j "${NCPUS}" LLVMDemangle
  echo "build.sh: RECIPE_QUICK_CHECK passed (cmake configure + LLVMDemangle)."
  exit 0
fi

ninja -j "${NCPUS}" clang clangInterpreter clangStaticAnalyzerCore

# compiler-rt is enabled solely for the OOP-JIT runtime that CppInterOp's
# clang-repl-based driver uses. We deliberately turn off every other
# compiler-rt component (sanitizers, fuzzer, profile, memprof, xray,
# gwp_asan, ctx_profile, builtins) so the build stays cheap — the asan
# *runtime* CppInterOp links against is whatever ships with the host
# clang, not what we'd be building here. The orc_rt target name varies
# per platform (orc_rt_osx, orc_rt_linux_x86_64, …); enumerate via
# `ninja -t targets` and build whatever matches, plus the executor.
#
# Why: `LLVM_USE_SANITIZER=Address;Undefined` propagates to *every* C/C++
# target in the build, including orc_rt and llvm-jitlink-executor.
# The OOP runtime artifacts therefore ship with asan/ubsan
# instrumentation baked in. This works in practice because CppInterOp's
# pre-existing asan row goes through the same path (see
# `Build_LLVM/action.yml` in CppInterOp before this migration), but be
# aware: a future change that lets the OOP runtime call into untrusted
# JIT'd code could expose double-instrumentation surprises. If a
# downstream consumer reports doubled asan reports, this is the place
# to start looking.
OOP_TARGETS=$(ninja -t targets all 2>/dev/null | \
  awk -F: '/^orc_rt[^:]*:/{print $1}' | sort -u | tr '\n' ' ')
if [[ -n "${OOP_TARGETS}" ]]; then
  ninja -j "${NCPUS}" llvm-jitlink-executor ${OOP_TARGETS}
else
  echo "build.sh: no orc_rt targets matched; OOP-JIT runtime won't be in the artifact." >&2
fi

# Trim the tree before rsync — keeps Releases asset under the per-asset
# 2 GB cap and matches what downstream consumers actually link against.
#
# What we keep:
#   - top-level build/, llvm/, clang/ (the only dirs consumers reach into)
#   - llvm/{include,lib,cmake} and clang/{include,lib,cmake} on the source side
#   - build/lib/*.{a,so,dylib} (link targets)
#   - build/lib/cmake/{llvm,clang}/ (find_package(LLVM)/find_package(Clang))
#   - build/include/ (generated config + tablegen .inc headers)
#   - build/tools/clang/include/ (clang's generated headers)
#   - build/bin/ (FileCheck, llvm-config, etc. that downstream tests need)
#
# What we drop, beyond the obvious source-side trim:
#   - build/**/CMakeFiles/   intermediate build state — every per-target
#                            *.dir/ subdir under here holds .o, .d, and
#                            cmake metadata. On asan builds these alone
#                            are 1-2 GB because asan-instrumented .o
#                            files are 3-5x the size of regular ones.
#                            Consumers never need them; we don't do
#                            incremental rebuilds (cache-or-rebuild model),
#                            so cmake's incremental scaffolding is dead
#                            weight in the artifact.
#   - build/.ninja_deps      ninja's incremental dep graph. Hundreds of
#     build/.ninja_log       MB on big builds; only useful for `ninja`
#                            re-runs we never do.
cd ..

shopt -s extglob
rm -rf -- !(build|llvm|clang)
( cd llvm  && rm -rf -- !(include|lib|cmake) )
( cd clang && rm -rf -- !(include|lib|cmake) )
( cd build && \
  rm -f compile_commands.json build.ninja .ninja_deps .ninja_log
  find . -name CMakeFiles -type d -prune -exec rm -rf {} + )
shopt -u extglob

rsync -a --delete \
  "$WORK_DIR/llvm-project/" "$OUT_DIR/llvm-project/"

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
