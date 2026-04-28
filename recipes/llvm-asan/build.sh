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
  -DLLVM_ENABLE_PROJECTS=clang \
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
  "${cmake_extra[@]}" \
  ../llvm

ninja -j "${NCPUS}" clang clangInterpreter clangStaticAnalyzerCore

# Trim the tree before rsync — keeps Releases asset under the per-asset
# 2 GB cap and matches what downstream consumers actually link against.
cd ..

shopt -s extglob
rm -rf -- !(build|llvm|clang)
( cd llvm  && rm -rf -- !(include|lib|cmake) )
( cd clang && rm -rf -- !(include|lib|cmake) )
( cd build && rm -f compile_commands.json build.ninja )
shopt -u extglob

rsync -a --delete \
  "$WORK_DIR/llvm-project/" "$OUT_DIR/llvm-project/"

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
