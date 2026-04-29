#!/usr/bin/env bash
# PR-time dry-run recipe. Goes through every shared step a real recipe
# uses (helper functions for env, cmake_extra, cleanup_intermediates,
# run_install_distribution; cache_pack via the publish-recipe action)
# but builds only LLVMDemangle so a hosted runner finishes in ~3-5 min
# instead of ~30. The verify.yml publish-dryrun matrix invokes this
# recipe via the real publish-recipe action with cache-base: file://
# so no upload happens — but tar+zstd+manifest+cache_upload all run
# against a real install tree.
#
# Inputs (env): see actions/lib/llvm-build.sh header.
#   RECIPE_VERSION         major LLVM version (matches release/{version}.x).
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

cmake_extra=()
while IFS= read -r line; do cmake_extra+=("$line"); done \
  < <(llvm_build::cmake_extra)

# Minimal LLVM-only configure. No clang, no compiler-rt: just enough
# for LLVMDemangle to build. host targets only — host;NVPTX would
# pull in extra deps we don't need here.
cmake -G Ninja \
  -DCMAKE_INSTALL_PREFIX="$OUT_DIR/llvm-project" \
  -DLLVM_TARGETS_TO_BUILD="host" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_TESTS=OFF \
  ${cmake_extra[@]+"${cmake_extra[@]}"} \
  ../llvm

ninja -j "${NCPUS}" LLVMDemangle

llvm_build::cleanup_intermediates

# Real recipes call llvm_build::install_distribution, which assembles a
# clang-centric umbrella list. This recipe ships only LLVMDemangle, so
# call run_install_distribution directly with a minimal scope.
# cmake-exports + llvm-headers install at configure time without
# requiring library builds.
llvm_build::run_install_distribution "LLVMDemangle;cmake-exports;llvm-headers"

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
