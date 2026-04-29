#!/usr/bin/env bash
# Builds an LLVM/Clang install tree with cling integrated as an
# LLVM_EXTERNAL_PROJECT. Source repos and branches are read from
# recipe.yaml so a tag bump there is the single point of edit.
#
# Recipe-specific bits live here (source clones, cmake flags, ninja
# targets, cling-specific post-install). The shared install-tree publish
# flow (env validation, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS,
# install-distribution, find_package smoke) lives in
# actions/lib/llvm-build.sh.
#
# Inputs (env): see actions/lib/llvm-build.sh header.
#   RECIPE_VERSION         flavor selector. Substitutes into
#                          recipe.yaml's source.branch_template
#                          ({version} → branch). Today this is one of
#                          'cling-llvm20' (minimal cling patches) or
#                          'ROOT-llvm20' (ROOT superset). Factored
#                          into the cache key, so each flavor has its
#                          own asset.
#
# Outputs (env, written to GITHUB_ENV when present):
#   SRC_COMMIT             sha of llvm-project HEAD that was built
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../actions/lib/llvm-build.sh
source "$SCRIPT_DIR/../../actions/lib/llvm-build.sh"

llvm_build::setup_env

# Pull source coordinates from recipe.yaml. Mirrors build-manifest.sh's
# grep approach — keeps the recipe.yaml content the single source of
# truth without dragging a YAML parser into the script.
LLVM_REPO=$(grep -E '^\s*repo:' "$SCRIPT_DIR/recipe.yaml" | sed -n '1p' | sed -E 's/.*repo:[[:space:]]*//' | tr -d '"')
LLVM_BRANCH_TPL=$(grep -E '^\s*branch_template:' "$SCRIPT_DIR/recipe.yaml" | head -1 | sed -E 's/.*branch_template:[[:space:]]*//' | tr -d "'\"")
CLING_REPO=$(grep -E '^\s*repo:' "$SCRIPT_DIR/recipe.yaml" | sed -n '2p' | sed -E 's/.*repo:[[:space:]]*//' | tr -d '"')
CLING_BRANCH=$(grep -E '^\s*branch:' "$SCRIPT_DIR/recipe.yaml" | head -1 | sed -E 's/.*branch:[[:space:]]*//' | tr -d '"')

[[ -n "$LLVM_REPO"       ]] || { echo "build.sh: source.repo missing in recipe.yaml" >&2; exit 1; }
[[ -n "$LLVM_BRANCH_TPL" ]] || { echo "build.sh: source.branch_template missing in recipe.yaml" >&2; exit 1; }
[[ -n "$CLING_REPO"      ]] || { echo "build.sh: cling.repo missing in recipe.yaml" >&2; exit 1; }
[[ -n "$CLING_BRANCH"    ]] || { echo "build.sh: cling.branch missing in recipe.yaml" >&2; exit 1; }

# {version} → flavor name. Same substitution build-manifest.sh does.
LLVM_BRANCH="${LLVM_BRANCH_TPL//\{version\}/$RECIPE_VERSION}"
echo "build.sh: flavor=${RECIPE_VERSION}; cloning ${LLVM_REPO}@${LLVM_BRANCH}"

cd "$WORK_DIR"
if [[ ! -d cling/.git ]]; then
  git clone --depth=1 -b "$CLING_BRANCH" "$CLING_REPO" cling
fi
if [[ ! -d llvm-project/.git ]]; then
  git clone --depth=1 -b "$LLVM_BRANCH" "$LLVM_REPO" llvm-project
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

# LLVM_EXTERNAL_PROJECTS=cling pulls cling's CMakeLists into the same
# build tree. cling's libraries get added to the build but cling uses
# raw `install(TARGETS ...)` rather than LLVM's add_llvm_install_targets,
# so its components have no install-X umbrellas — they're installed
# separately below via cmake --install --component, which doesn't
# require the umbrella.
cmake -G Ninja \
  -DCMAKE_INSTALL_PREFIX="$OUT_DIR/llvm-project" \
  -DLLVM_ENABLE_PROJECTS="clang" \
  -DLLVM_EXTERNAL_PROJECTS=cling \
  -DLLVM_EXTERNAL_CLING_SOURCE_DIR="$WORK_DIR/cling" \
  -DLLVM_TARGETS_TO_BUILD="host;NVPTX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DCLANG_ENABLE_STATIC_ANALYZER=OFF \
  -DCLANG_ENABLE_ARCMT=OFF \
  -DCLANG_ENABLE_FORMAT=OFF \
  -DCLANG_ENABLE_BOOTSTRAP=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_TESTS=OFF \
  ${cmake_extra[@]+"${cmake_extra[@]}"} \
  ../llvm

llvm_build::quick_check_or_continue

# Build clang driver + clang-repl Interpreter library (downstream ROOT
# consumes libclangInterpreter.a even though the clang driver doesn't
# depend on it transitively) + StaticAnalyzerCore (cling-bundled clang
# pulled it into CppInterOp's link in the past) + LLVMOrcDebugging
# (cling pulls it via LIBS) + clingInterpreter (the cling library; the
# `cling` binary follows transitively).
ninja -j "${NCPUS}" clang clangInterpreter clangStaticAnalyzerCore \
                    LLVMOrcDebugging clingInterpreter

llvm_build::cleanup_intermediates

# Cling components must NOT be in DIST_COMPONENTS — install-distribution
# requires install-X umbrellas that cling's raw install(TARGETS) doesn't
# create. Pass no extras here; install LLVM/clang via the helper, then
# install cling separately below.
llvm_build::install_distribution

# Cling components — installed separately because cling's cmake doesn't
# create install-X umbrellas. cmake --install --component runs the
# install rule directly. Walk libcling*.a / cling*.lib (the latter is
# Windows-MSVC convention) to discover what cling built.
for f in lib/libcling*.a lib/cling*.lib; do
  [[ -f "$f" ]] || continue
  comp=$(basename "$f")
  comp="${comp#lib}"; comp="${comp%.a}"; comp="${comp%.lib}"
  cmake --install . --component "$comp" 2>/dev/null \
    || echo "build.sh: cling component $comp install rule absent" >&2
done
# `cling` binary: cling's CMakeLists install(TARGETS cling RUNTIME ...)
# uses COMPONENT cling. If that path doesn't exist (older cling), fall
# back to a manual copy.
if cmake --install . --component cling 2>/dev/null; then
  :
elif [[ -x bin/cling ]]; then
  install -m 0755 bin/cling "$OUT_DIR/llvm-project/bin/"
elif [[ -f bin/cling.exe ]]; then
  cp bin/cling.exe "$OUT_DIR/llvm-project/bin/"
fi
# Cling headers — cling's install rules typically don't ship them
# (consumers historically read from the source tree). Stage them under
# include/cling/ in the install tree so consumers find them without an
# extra source clone.
if [[ -d "$WORK_DIR/cling/include/cling" ]]; then
  mkdir -p "$OUT_DIR/llvm-project/include"
  cp -R "$WORK_DIR/cling/include/cling" "$OUT_DIR/llvm-project/include/"
fi

# Producer-side smoke. find_package(LLVM)+(Clang) covers the LLVM/clang
# install; cling has no Config.cmake, so add a libclingInterpreter
# existence check (the matching .lib name on Windows MSVC is
# clingInterpreter.lib — without the lib prefix).
if [[ -f "$OUT_DIR/llvm-project/lib/libclingInterpreter.a" ]]; then
  llvm_build::smoke "lib/libclingInterpreter.a"
elif [[ -f "$OUT_DIR/llvm-project/lib/clingInterpreter.lib" ]]; then
  llvm_build::smoke "lib/clingInterpreter.lib"
else
  echo "::error::neither libclingInterpreter.a nor clingInterpreter.lib found in install"
  exit 1
fi

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
