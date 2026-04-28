#!/usr/bin/env bash
# Builds an asan+ubsan-instrumented Clang/LLVM install tree.
#
# We publish a cmake --install tree (not a build tree). LLVMConfig.cmake
# in an install tree uses _IMPORT_PREFIX-relative paths
# (`set(_IMPORT_PREFIX "${CMAKE_CURRENT_LIST_DIR}/../../..")`, generated
# by cmake's install(EXPORT) — see
# https://cmake.org/cmake/help/latest/command/install.html#export), so
# the consumer can extract the asset under any path. Build trees bake
# in absolute paths from configure-time and are not relocatable; that's
# what package-manager LLVM avoids and what we copy.
#
# Inputs (env):
#   RECIPE_VERSION         major LLVM version, e.g. 22
#   WORK_DIR               scratch directory (clone + build live here)
#   OUT_DIR                CMAKE_INSTALL_PREFIX is $OUT_DIR/llvm-project;
#                          the install lands directly there for tar/upload.
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

# Free disk before the install phase. asan-instrumented .o files are
# the bulk of the build tree (3-5x larger than vanilla); a hosted
# Linux runner has ~14 GiB free disk and the recipe's intermediate
# state crowds it. ccache has already captured every compile we care
# about by this point — its hit/miss key is the source + flags, not
# the .o on disk — so deleting *.o doesn't lose ccache state. The
# install phase below copies .a / binaries / headers / cmake-exports
# only; none of it reaches into .o.
echo "build.sh: pre-install disk: $(df -h . | tail -1)"
echo "build.sh: dropping intermediate .o files"
find . -name '*.o' -delete
echo "build.sh: post-cleanup disk: $(df -h . | tail -1)"

# Drive the install through LLVM_DISTRIBUTION_COMPONENTS, not raw
# `cmake --install --component`. The naive per-component approach
# installs only built libraries, but `cmake-exports` always emits a
# *complete* LLVMExports.cmake that lists every LLVM library the build
# tree configured (including ones we never built — LLVMSupportLSP,
# LLVMDiff, LLVMDebuginfod, …). find_package(LLVM) walks
# LLVMExports.cmake at consumer time and aborts on any IMPORTED target
# whose .a is missing, which is exactly what bit CppInterOp's asan row
# the first time it tried to consume the install tree.
#
# LLVM_DISTRIBUTION_COMPONENTS scopes the cmake-exports output: when
# set, install_distribution_exports() in LLVMDistributionSupport.cmake
# emits an LLVMExports.cmake containing only the listed components. So
# we compute the list from what we actually built (libclang*.a +
# libLLVM*.a + OOP_TARGETS + the umbrella headers/exports), reconfigure
# with that as DISTRIBUTION_COMPONENTS, then `ninja install-distribution`
# installs only those and writes a self-consistent exports file.
#
# Reconfigure cost is ~3-5 s (cmake re-emits build.ninja); install-
# distribution is fast because every listed component is already built.
#
# See https://llvm.org/docs/BuildingADistribution.html#options-for-building-an-llvm-distribution
# and llvm/cmake/modules/LLVMDistributionSupport.cmake.

# Umbrellas + computed per-library list. Headers/cmake-exports umbrellas
# don't suffer the missing-.a problem because they install source-tree
# or generated files, not built libraries.
declare -a DIST_COMPONENTS=(
  clang
  clang-headers
  clang-cmake-exports
  clang-resource-headers
  clangInterpreter
  cmake-exports
  llvm-headers
  llvm-config
)
for f in lib/libclang*.a lib/libLLVM*.a; do
  [[ -f "$f" ]] || continue
  DIST_COMPONENTS+=("$(basename "$f" | sed 's/^lib//; s/\.a$//')")
done
for tgt in ${OOP_TARGETS}; do
  DIST_COMPONENTS+=("$tgt")
done

DIST_STR=$(IFS=';'; echo "${DIST_COMPONENTS[*]}")
echo "build.sh: LLVM_DISTRIBUTION_COMPONENTS=${DIST_STR}"
cmake -DLLVM_DISTRIBUTION_COMPONENTS="${DIST_STR}" .

ninja -j "${NCPUS}" install-distribution

# llvm-jitlink-executor's CMakeLists registers an install() rule but
# its COMPONENT defaults to "Unspecified", so it can't be in
# DISTRIBUTION_COMPONENTS. Copy by hand into the install bin/ so
# consumers that need the OOP executor find it next to clang at
# $LLVM/bin/llvm-jitlink-executor.
if [[ -x bin/llvm-jitlink-executor ]]; then
  install -m 0755 bin/llvm-jitlink-executor "$OUT_DIR/llvm-project/bin/"
fi

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
