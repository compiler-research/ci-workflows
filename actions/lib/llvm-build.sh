#!/usr/bin/env bash
# llvm-build.sh — shared scaffolding for LLVM-family recipes.
#
# Recipes source this file and call its functions to handle the bits
# every LLVM install-tree publish does the same way (env validation,
# cmake-extra plumbing, .o cleanup, LLVM_DISTRIBUTION_COMPONENTS
# computation, install-distribution, find_package smoke). Recipe-
# specific work (source clone, cmake flags, ninja targets, post-install
# hooks) stays in the recipe's own build.sh.
#
# Required env (every recipe; helpers `: ${X:?}`-assert these):
#   RECIPE_VERSION   recipe-defined version/flavor selector
#   WORK_DIR         scratch directory; clone + build live here
#   OUT_DIR          install prefix is "$OUT_DIR/llvm-project"; the
#                    install lands directly there for tar/upload.
#
# Optional env:
#   NCPUS                            parallelism; default = nproc
#   CMAKE_C_COMPILER_LAUNCHER        passed through (e.g. ccache)
#   CMAKE_CXX_COMPILER_LAUNCHER      passed through
#   CMAKE_C_COMPILER, CMAKE_CXX_COMPILER  override compiler if set
#
# FIXME(port-to-python): same FIXME as cache-io.sh and bin/recipe-cache.
# Long-term we move to stdlib Python; bash works on Linux/macOS today
# and on Windows via git-bash.

# Validate required env vars and ensure WORK_DIR / OUT_DIR exist.
# Sets NCPUS if not already set; tries getconf then sysctl then 4.
llvm_build::setup_env() {
  : "${RECIPE_VERSION:?RECIPE_VERSION must be set}"
  : "${WORK_DIR:?WORK_DIR must be set}"
  : "${OUT_DIR:?OUT_DIR must be set}"
  NCPUS="${NCPUS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null \
                    || sysctl -n hw.logicalcpu 2>/dev/null \
                    || echo 4)}"
  export NCPUS
  mkdir -p "$WORK_DIR" "$OUT_DIR"
}

# Emit -DCMAKE_*_COMPILER[_LAUNCHER]= flags, one per line, for cmake_extra.
# Recipes capture into an array (bash 3.2-portable; mapfile would be
# cleaner but isn't on macOS /bin/bash):
#   cmake_extra=()
#   while IFS= read -r line; do cmake_extra+=("$line"); done \
#     < <(llvm_build::cmake_extra)
# and splat into the cmake invocation as
#   ${cmake_extra[@]+"${cmake_extra[@]}"}
# The set+expand-if-defined idiom is bash 3.2-required: under
# `set -u`, bash 3.2 treats an empty array as unset and errors on
# plain "${cmake_extra[@]}".
llvm_build::cmake_extra() {
  [[ -n "${CMAKE_C_COMPILER_LAUNCHER:-}" ]] && \
    printf -- '-DCMAKE_C_COMPILER_LAUNCHER=%s\n' "$CMAKE_C_COMPILER_LAUNCHER"
  [[ -n "${CMAKE_CXX_COMPILER_LAUNCHER:-}" ]] && \
    printf -- '-DCMAKE_CXX_COMPILER_LAUNCHER=%s\n' "$CMAKE_CXX_COMPILER_LAUNCHER"
  [[ -n "${CMAKE_C_COMPILER:-}" ]] && \
    printf -- '-DCMAKE_C_COMPILER=%s\n' "$CMAKE_C_COMPILER"
  [[ -n "${CMAKE_CXX_COMPILER:-}" ]] && \
    printf -- '-DCMAKE_CXX_COMPILER=%s\n' "$CMAKE_CXX_COMPILER"
  return 0
}

# Handle RECIPE_QUICK_CHECK=1: build LLVMDemangle as a host-toolchain
# smoke and exit. Verify.yml runs this across every cells.yaml entry
# on its native OS, so adding a cell or recipe automatically gets the
# configure + LLVMDemangle compile path exercised at PR time on the
# right runner.
#
# Catches:
#   - host toolchain misconfiguration on the matrix runner (e.g.
#     MinGW g++ on Windows when the recipe needs MSVC; LLVMDemangle
#     fails to compile because the wrong cl/g++ rejects something);
#   - cmake configure regressions on a particular OS (recipe-specific
#     flags rejected, missing system deps);
#   - cells.yaml referencing a recipe that doesn't configure on the
#     named OS at all.
#
# Tried extending this with a minimal install-distribution +
# find_package smoke to also catch LLVMConfig load-time issues, but
# the post-LLVMDemangle reconfigure with LLVM_DISTRIBUTION_COMPONENTS
# bumps llvm-config.h, which cascades into rebuilding LLVMSupport's
# 241 sources — defeating the "quick" property. Cling-enabled recipes
# also break it, since cling's standalone install(EXPORT ClingTargets)
# isn't under LLVM's distribution machinery and references LLVM
# targets that get scoped out. The full publish smoke (every cell
# after merge) remains the catch for those classes.
llvm_build::quick_check_or_continue() {
  [[ "${RECIPE_QUICK_CHECK:-0}" == "1" ]] || return 0
  ninja -j "${NCPUS}" LLVMDemangle
  echo "build.sh: RECIPE_QUICK_CHECK passed (cmake configure + LLVMDemangle)."
  exit 0
}

# Drop intermediate object files. ccache state is unaffected (ccache
# keys on source+flags, not the .o on disk). Frees several GiB of disk
# headroom before the install phase, which historically pushed hosted
# Linux runners over their 14 GiB free-disk budget on asan-instrumented
# builds. Cwd should be the build directory; matches both .o (Linux/
# macOS GNU/clang) and .obj (Windows MSVC).
llvm_build::cleanup_intermediates() {
  if command -v df >/dev/null 2>&1; then
    echo "build.sh: pre-install disk: $(df -h . | tail -1)"
  fi
  echo "build.sh: dropping intermediate object files"
  find . \( -name '*.o' -o -name '*.obj' \) -delete
  if command -v df >/dev/null 2>&1; then
    echo "build.sh: post-cleanup disk: $(df -h . | tail -1)"
  fi
}

# Compute LLVM_DISTRIBUTION_COMPONENTS from the umbrellas every install
# tree wants plus the libraries that actually exist on disk plus any
# extra components the recipe passes in (orc_rt platform variants,
# etc.). Reconfigure cmake with that list so install-distribution
# scopes LLVMExports.cmake / ClangExports.cmake to exactly what we
# ship. Then install-distribution.
#
# Walk both *.a (Linux/macOS) and *.lib (Windows MSVC); strip optional
# `lib` prefix so component names match cmake target names regardless
# of the host's static-archive convention.
#
# See https://llvm.org/docs/BuildingADistribution.html and
# llvm/cmake/modules/LLVMDistributionSupport.cmake for the exports-
# scoping behavior this relies on.
#
# Args: extra component names (variadic). Cwd: build directory.
llvm_build::install_distribution() {
  local -a dist=(
    clang
    clang-headers
    clang-cmake-exports
    clang-resource-headers
    clangInterpreter
    cmake-exports
    llvm-headers
    llvm-config
  )
  local f base
  for f in lib/libclang*.a lib/libLLVM*.a lib/clang*.lib lib/LLVM*.lib; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f")
    base="${base#lib}"
    base="${base%.a}"
    base="${base%.lib}"
    dist+=("$base")
  done
  dist+=("$@")

  local IFS=';'
  llvm_build::run_install_distribution "${dist[*]}"
}

# Lower-level helper: take a literal semicolon-joined component string,
# reconfigure cmake with LLVM_DISTRIBUTION_COMPONENTS, install each
# component. Recipes that don't follow the umbrella convention (e.g.
# llvm-dry-run, which ships only LLVMDemangle and can't include
# `clang`-prefixed umbrellas the way real recipes do) call this
# directly. Real recipes go through llvm_build::install_distribution
# which builds the umbrella+walk list and delegates here.
#
# We deliberately avoid `ninja install-distribution`: that target
# depends on every library in the configured project being built,
# not just the libraries listed in LLVM_DISTRIBUTION_COMPONENTS.
# On a partial build (e.g. llvm-dry-run, which only ninja-builds
# LLVMDemangle), `ninja install-distribution` cascades into
# building LLVMSupport's ~240 sources etc. — defeating the dry-run's
# fast-feedback property. `cmake --install --component` runs the
# install rule directly with no build-side dependency, so it only
# touches files that are already on disk.
#
# Cwd: build directory.
llvm_build::run_install_distribution() {
  local dist_str="$1"
  echo "build.sh: LLVM_DISTRIBUTION_COMPONENTS=${dist_str}"
  cmake -DLLVM_DISTRIBUTION_COMPONENTS="${dist_str}" .
  local IFS=';'
  local comp
  for comp in $dist_str; do
    cmake --install . --component "$comp"
  done
}

# Producer-side smoke: invoke find_package(LLVM REQUIRED) and
# find_package(Clang REQUIRED) from a throwaway cmake project against
# the install tree. find_package walks every IMPORTED target's
# IMPORTED_LOCATION_RELEASE and validates the file exists, plus checks
# every reference in INTERFACE_LINK_LIBRARIES — exactly what the
# consumer will run. Catches missing-.a-in-exports inconsistency
# before the asset is tar'd and uploaded.
#
# C is enabled alongside CXX because LLVMConfig.cmake's
# find_package(LibEdit) calls check_include_file(histedit.h ...),
# which try_compile()s a generated .c file. Without C, cmake aborts
# with "try_compile() works only for enabled languages. Currently
# these are: CXX" — observed on macOS publishes.
#
# Args: extra existence checks, each a path relative to the install
# prefix that must exist (e.g. "lib/libclingInterpreter.a"). Used by
# recipes whose products aren't covered by find_package (cling has no
# ClingConfig.cmake). Cwd: anywhere.
llvm_build::smoke() {
  local smoke_dir status=0
  smoke_dir="$(mktemp -d)"
  # No trap '... RETURN' here: the RETURN trap is bash 4+ and macOS
  # /bin/bash is 3.2. Track status manually and `rm -rf` at the end.

  cat > "$smoke_dir/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(install_tree_smoke LANGUAGES C CXX)
find_package(LLVM REQUIRED CONFIG PATHS "${SMOKE_LLVM_PREFIX}/lib/cmake/llvm" NO_DEFAULT_PATH)
find_package(Clang REQUIRED CONFIG PATHS "${SMOKE_LLVM_PREFIX}/lib/cmake/clang" NO_DEFAULT_PATH)
message(STATUS "smoke: LLVM ${LLVM_VERSION_MAJOR}.${LLVM_VERSION_MINOR}.${LLVM_VERSION_PATCH} loaded from ${LLVM_DIR}")
foreach(rel IN LISTS SMOKE_REQUIRED_FILES)
  if(rel AND NOT EXISTS "${SMOKE_LLVM_PREFIX}/${rel}")
    message(FATAL_ERROR "smoke: required file missing from install tree: ${rel}")
  endif()
endforeach()
EOF

  local required_files=""
  if [[ $# -gt 0 ]]; then
    local IFS=';'
    required_files="$*"
  fi

  echo "build.sh: smoke-testing install tree (find_package LLVM + Clang${1:+ + existence: $*})"
  if ! cmake -S "$smoke_dir" -B "$smoke_dir/build" \
       -DSMOKE_LLVM_PREFIX="$OUT_DIR/llvm-project" \
       -DSMOKE_REQUIRED_FILES="${required_files}" \
       >"$smoke_dir/log" 2>&1; then
    echo "::error::install tree failed find_package smoke. Exports likely reference missing files."
    tail -50 "$smoke_dir/log" >&2
    status=1
  fi
  rm -rf "$smoke_dir"
  [[ $status -eq 0 ]] && echo "build.sh: smoke passed."
  return $status
}
