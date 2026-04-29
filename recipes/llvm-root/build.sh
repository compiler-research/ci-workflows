#!/usr/bin/env bash
# Builds an LLVM/Clang install tree with cling integrated as an
# LLVM_EXTERNAL_PROJECT. Source repos and branches are read from
# recipe.yaml so a tag bump there is the single point of edit.
#
# We publish a cmake --install tree (not a build tree). LLVMConfig.cmake
# in an install tree uses _IMPORT_PREFIX-relative paths so the consumer
# can extract the asset under any path; build trees bake in absolute
# paths from configure-time and are not relocatable. See the
# llvm-asan recipe for the pattern this is modelled on.
#
# Inputs (env):
#   RECIPE_VERSION         flavor selector. Substitutes into
#                          recipe.yaml's source.branch_template
#                          ({version} → branch). Today this is one of
#                          'cling-llvm20' (minimal cling patches) or
#                          'ROOT-llvm20' (ROOT superset). Factored
#                          into the cache key, so each flavor has its
#                          own asset.
#   WORK_DIR               scratch directory (clones + build live here)
#   OUT_DIR                CMAKE_INSTALL_PREFIX is $OUT_DIR/llvm-project
#   NCPUS                  parallelism (default: nproc)
#   CMAKE_C_COMPILER_LAUNCHER, CMAKE_CXX_COMPILER_LAUNCHER
#                          optional; passed through to cmake (ccache).
#
# Outputs (env, written to GITHUB_ENV when present):
#   SRC_COMMIT             sha of llvm-project HEAD that was built
set -euo pipefail

: "${RECIPE_VERSION:?RECIPE_VERSION must be set}"
: "${WORK_DIR:?WORK_DIR must be set}"
: "${OUT_DIR:?OUT_DIR must be set}"
NCPUS="${NCPUS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.logicalcpu 2>/dev/null || echo 4)}"

RECIPE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Pull source coordinates from recipe.yaml. Mirrors build-manifest.sh's
# grep approach — keeps the recipe.yaml content the single source of
# truth without dragging a YAML parser into the script.
LLVM_REPO=$(grep -E '^\s*repo:' "$RECIPE_DIR/recipe.yaml" | sed -n '1p' | sed -E 's/.*repo:[[:space:]]*//' | tr -d '"')
LLVM_BRANCH_TPL=$(grep -E '^\s*branch_template:' "$RECIPE_DIR/recipe.yaml" | head -1 | sed -E 's/.*branch_template:[[:space:]]*//' | tr -d "'\"")
CLING_REPO=$(grep -E '^\s*repo:' "$RECIPE_DIR/recipe.yaml" | sed -n '2p' | sed -E 's/.*repo:[[:space:]]*//' | tr -d '"')
CLING_BRANCH=$(grep -E '^\s*branch:' "$RECIPE_DIR/recipe.yaml" | head -1 | sed -E 's/.*branch:[[:space:]]*//' | tr -d '"')

[[ -n "$LLVM_REPO"       ]] || { echo "build.sh: source.repo missing in recipe.yaml" >&2; exit 1; }
[[ -n "$LLVM_BRANCH_TPL" ]] || { echo "build.sh: source.branch_template missing in recipe.yaml" >&2; exit 1; }
[[ -n "$CLING_REPO"      ]] || { echo "build.sh: cling.repo missing in recipe.yaml" >&2; exit 1; }
[[ -n "$CLING_BRANCH"    ]] || { echo "build.sh: cling.branch missing in recipe.yaml" >&2; exit 1; }

# {version} → flavor name. Same substitution build-manifest.sh does.
LLVM_BRANCH="${LLVM_BRANCH_TPL//\{version\}/$RECIPE_VERSION}"
echo "build.sh: flavor=${RECIPE_VERSION}; cloning ${LLVM_REPO}@${LLVM_BRANCH}"

mkdir -p "$WORK_DIR" "$OUT_DIR"
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

cmake_extra=()
[[ -n "${CMAKE_C_COMPILER_LAUNCHER:-}" ]] && \
  cmake_extra+=( -DCMAKE_C_COMPILER_LAUNCHER="${CMAKE_C_COMPILER_LAUNCHER}" )
[[ -n "${CMAKE_CXX_COMPILER_LAUNCHER:-}" ]] && \
  cmake_extra+=( -DCMAKE_CXX_COMPILER_LAUNCHER="${CMAKE_CXX_COMPILER_LAUNCHER}" )
[[ -n "${CMAKE_C_COMPILER:-}" ]] && \
  cmake_extra+=( -DCMAKE_C_COMPILER="${CMAKE_C_COMPILER}" )
[[ -n "${CMAKE_CXX_COMPILER:-}" ]] && \
  cmake_extra+=( -DCMAKE_CXX_COMPILER="${CMAKE_CXX_COMPILER}" )

# LLVM_EXTERNAL_PROJECTS=cling pulls cling's CMakeLists into the same
# build tree. cling's libraries (clingInterpreter, clingMetaProcessor,
# clingUtils) get added to LLVM's install machinery and become
# install-X umbrella targets like any in-tree clang library.
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
  "${cmake_extra[@]}" \
  ../llvm

if [[ "${RECIPE_QUICK_CHECK:-0}" == "1" ]]; then
  ninja -j "${NCPUS}" LLVMDemangle
  echo "build.sh: RECIPE_QUICK_CHECK passed (cmake configure + LLVMDemangle)."
  exit 0
fi

# Build the clang driver, the clang-repl Interpreter library
# (downstream ROOT consumes it via libclangInterpreter.a even though
# clang-the-binary doesn't depend on it transitively), the
# StaticAnalyzerCore library (cling-bundled clang dragged it into
# CppInterOp's link in the past), LLVMOrcDebugging (cling pulls it
# in via LIBS in current cling tip), and the cling library itself
# (clingInterpreter; the `cling` binary follows transitively).
ninja -j "${NCPUS}" clang clangInterpreter clangStaticAnalyzerCore \
                    LLVMOrcDebugging clingInterpreter

# Free disk before the install phase. Same reasoning as llvm-asan:
# ccache has captured every compile by this point, so deleting *.o
# doesn't lose any cache state, and the install phase only copies
# .a / binaries / headers / cmake-exports.
echo "build.sh: pre-install disk: $(df -h . | tail -1)"
echo "build.sh: dropping intermediate .o files"
find . -name '*.o' -delete
echo "build.sh: post-cleanup disk: $(df -h . | tail -1)"

# Drive the LLVM/Clang install through LLVM_DISTRIBUTION_COMPONENTS so
# the generated LLVMExports.cmake / ClangExports.cmake only reference
# libraries we actually shipped. Same pattern as llvm-asan.
#
# Cling's cmake uses raw `install(TARGETS ...)` instead of LLVM's
# `add_llvm_install_targets`, which means cling components have no
# `install-X` umbrella targets. install-distribution requires those
# umbrellas, so cling components must NOT be in DIST_COMPONENTS — we
# install them separately below via `cmake --install --component`,
# which runs the install rule directly without needing an umbrella.
# Likewise, the per-library walk skips lib/libcling*.a.
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

DIST_STR=$(IFS=';'; echo "${DIST_COMPONENTS[*]}")
echo "build.sh: LLVM_DISTRIBUTION_COMPONENTS=${DIST_STR}"
cmake -DLLVM_DISTRIBUTION_COMPONENTS="${DIST_STR}" .

ninja -j "${NCPUS}" install-distribution

# Cling components — installed separately because cling's cmake doesn't
# create install-X umbrellas. cmake --install --component runs the
# install rule directly. Walk libcling*.a to discover what cling built;
# the cling binary lands via its own component.
for f in lib/libcling*.a; do
  [[ -f "$f" ]] || continue
  comp=$(basename "$f" | sed 's/^lib//; s/\.a$//')
  cmake --install . --component "$comp" 2>/dev/null \
    || echo "build.sh: cling component $comp install rule absent" >&2
done
# `cling` binary: cling's CMakeLists install(TARGETS cling RUNTIME ...)
# uses COMPONENT cling. If that path doesn't exist (older cling),
# fall back to a manual copy.
if cmake --install . --component cling 2>/dev/null; then
  :
elif [[ -x bin/cling ]]; then
  install -m 0755 bin/cling "$OUT_DIR/llvm-project/bin/"
fi
# Cling headers — cling's install rules typically don't ship them
# (consumers historically read from the source tree via include path).
# Stage them under include/cling/ in the install tree so consumers
# find them without an extra source clone.
if [[ -d "$WORK_DIR/cling/include/cling" ]]; then
  mkdir -p "$OUT_DIR/llvm-project/include"
  cp -R "$WORK_DIR/cling/include/cling" "$OUT_DIR/llvm-project/include/"
fi

# Producer-side smoke: find_package(LLVM)+(Clang) from a throwaway
# cmake project against the install tree. Catches any missing-.a-in-
# exports inconsistency before the asset is tar'd. Cling does not
# ship a ClingConfig.cmake (cling is consumed via header includes +
# direct linkage against libclingInterpreter.a, not find_package), so
# we don't add it to the smoke. ~5 s.
SMOKE_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_DIR"' EXIT
cat > "$SMOKE_DIR/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(install_tree_smoke LANGUAGES CXX)
find_package(LLVM REQUIRED CONFIG PATHS "${SMOKE_LLVM_PREFIX}/lib/cmake/llvm" NO_DEFAULT_PATH)
find_package(Clang REQUIRED CONFIG PATHS "${SMOKE_LLVM_PREFIX}/lib/cmake/clang" NO_DEFAULT_PATH)
message(STATUS "smoke: LLVM ${LLVM_VERSION_MAJOR}.${LLVM_VERSION_MINOR}.${LLVM_VERSION_PATCH} loaded from ${LLVM_DIR}")
# Spot-check that libclingInterpreter.a actually shipped — find_package(LLVM)
# alone doesn't validate cling-specific files since cling has no Config.cmake.
if(NOT EXISTS "${SMOKE_LLVM_PREFIX}/lib/libclingInterpreter.a")
  message(FATAL_ERROR "smoke: libclingInterpreter.a missing from install tree")
endif()
EOF
echo "build.sh: smoke-testing install tree (find_package LLVM + Clang + cling lib)"
cmake -S "$SMOKE_DIR" -B "$SMOKE_DIR/build" \
  -DSMOKE_LLVM_PREFIX="$OUT_DIR/llvm-project" \
  >"$SMOKE_DIR/log" 2>&1 || {
  echo "::error::install tree failed find_package smoke. Exports likely reference missing files."
  tail -50 "$SMOKE_DIR/log" >&2
  exit 1
}
echo "build.sh: smoke passed."

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
