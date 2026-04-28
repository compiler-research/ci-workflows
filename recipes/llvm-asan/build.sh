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
  # Dry-run every install umbrella we depend on. ninja -n parses the
  # build graph and exits non-zero on an unknown target, without doing
  # any work — so an upstream LLVM rename of `install-clang-libraries`,
  # `install-cmake-exports`, etc. fails this 5-second smoke instead of
  # the ~30-min publish post-merge.
  ninja -n \
    install-clang \
    install-clang-libraries \
    install-clang-headers \
    install-clang-cmake-exports \
    install-clang-resource-headers \
    install-clangInterpreter \
    install-llvm-libraries \
    install-llvm-headers \
    install-cmake-exports \
    install-llvm-config \
    >/dev/null
  echo "build.sh: RECIPE_QUICK_CHECK passed (cmake configure + LLVMDemangle + install-target dry-run)."
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

# Install the umbrella components consumers reach into. Each `install-X`
# is a phony ninja target that copies one component group's headers,
# libraries, cmake-exports, etc. into $CMAKE_INSTALL_PREFIX. Cherry-
# picked rather than running plain `ninja install` so the asset doesn't
# pull in LLVM tooling (opt, llc, llvm-dis, …) we never load. See
# https://llvm.org/docs/BuildingADistribution.html for the umbrella
# convention.
INSTALL_TARGETS=(
  install-clang
  install-clang-libraries
  install-clang-headers
  install-clang-cmake-exports
  install-clang-resource-headers
  install-clangInterpreter
  install-llvm-libraries
  install-llvm-headers
  install-cmake-exports
  install-llvm-config
)
# install-orc_rt_<platform> targets mirror the OOP_TARGETS we just built.
for tgt in ${OOP_TARGETS}; do
  INSTALL_TARGETS+=("install-${tgt}")
done

ninja -j "${NCPUS}" "${INSTALL_TARGETS[@]}"

# llvm-jitlink-executor's CMakeLists registers an install() rule but no
# `install-llvm-jitlink-executor` umbrella target. Copy by hand into the
# install bin/ so consumers that need the OOP executor find it next to
# clang at $LLVM/bin/llvm-jitlink-executor.
if [[ -x bin/llvm-jitlink-executor ]]; then
  install -m 0755 bin/llvm-jitlink-executor "$OUT_DIR/llvm-project/bin/"
fi

echo "build.sh: done. SRC_COMMIT=${SRC_COMMIT}"
