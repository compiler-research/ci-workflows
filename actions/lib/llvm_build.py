"""Shared scaffolding for LLVM-family recipes.

Python port of actions/lib/llvm-build.sh. Recipes import this module
and call its functions to handle the bits every LLVM install-tree
publish does the same way (env validation, cmake-extra plumbing,
intermediate cleanup, LLVM_DISTRIBUTION_COMPONENTS computation,
install-distribution, find_package smoke). Recipe-specific work
(source clone, cmake flags, ninja targets, post-install hooks) stays
in the recipe's own build.py.

Required env (every recipe; setup_env asserts these):
  RECIPE_VERSION   recipe-defined version/flavor selector
  WORK_DIR         scratch directory; clone + build live here
  OUT_DIR          install prefix is "$OUT_DIR/llvm-project"; the
                   install lands directly there for tar/upload.

Optional env:
  NCPUS                            parallelism; default = nproc
  CMAKE_C_COMPILER_LAUNCHER        passed through (e.g. ccache)
  CMAKE_CXX_COMPILER_LAUNCHER      passed through
  CMAKE_C_COMPILER, CMAKE_CXX_COMPILER  override compiler if set
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_env() -> None:
    """Validate required env vars and ensure WORK_DIR / OUT_DIR exist.

    Sets NCPUS env if not already set; tries os.cpu_count() then 4.
    """
    for name in ("RECIPE_VERSION", "WORK_DIR", "OUT_DIR"):
        if not os.environ.get(name):
            raise EnvironmentError(f"{name} must be set")
    if not os.environ.get("NCPUS"):
        os.environ["NCPUS"] = str(os.cpu_count() or 4)
    Path(os.environ["WORK_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(os.environ["OUT_DIR"]).mkdir(parents=True, exist_ok=True)


def base_cmake_args(install_prefix: str,
                    targets: str = "host;NVPTX") -> List[str]:
    """Cmake flags every LLVM-family recipe shares.

    Recipes append their flavor-specific flags (LLVM_USE_SANITIZER for
    asan, LLVM_EXTERNAL_PROJECTS=cling for root, etc.) and feed the
    combined list to cmake. Centralising the shared subset means a flag
    bump happens in one place, not three.
    """
    return [
        "cmake", "-G", "Ninja",
        f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
        f"-DLLVM_TARGETS_TO_BUILD={targets}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_ENABLE_ASSERTIONS=ON",
        "-DCLANG_ENABLE_STATIC_ANALYZER=OFF",
        "-DCLANG_ENABLE_ARCMT=OFF",
        "-DCLANG_ENABLE_FORMAT=OFF",
        "-DCLANG_ENABLE_BOOTSTRAP=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF",
    ]


def clone_shallow(repo: str, branch: str, dest: Path) -> None:
    """Shallow git clone to `dest` if missing. No-op on a re-run with
    a populated working tree (ccache + actions/cache reuse the workspace).
    """
    if (dest / ".git").is_dir():
        return
    subprocess.run(
        ["git", "clone", "--depth=1", "-b", branch, repo, str(dest)],
        check=True,
    )


def record_src_commit(repo_path: Path) -> str:
    """Return the HEAD sha of `repo_path` and append it to $GITHUB_ENV.

    The action.yml uses `SRC_COMMIT` as a recipe-output env so
    publish-recipe can stamp it into the manifest's source.commit.
    """
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    github_env = os.environ.get("GITHUB_ENV", "")
    if github_env:
        with open(github_env, "a") as f:
            f.write(f"SRC_COMMIT={sha}\n")
    return sha


def cmake_extra() -> List[str]:
    """Return cmake -D flags derived from CC/CXX/launcher env vars."""
    flags: List[str] = []
    for env_name, flag in (
        ("CMAKE_C_COMPILER_LAUNCHER",   "-DCMAKE_C_COMPILER_LAUNCHER"),
        ("CMAKE_CXX_COMPILER_LAUNCHER", "-DCMAKE_CXX_COMPILER_LAUNCHER"),
        ("CMAKE_C_COMPILER",            "-DCMAKE_C_COMPILER"),
        ("CMAKE_CXX_COMPILER",          "-DCMAKE_CXX_COMPILER"),
    ):
        v = os.environ.get(env_name, "")
        if v:
            flags.append(f"{flag}={v}")
    return flags


def record_cmake_args(args: Sequence[str]) -> None:
    """Persist the cmake invocation for build_manifest to inline.

    Consumers compare against their own recipe-derived flags to catch
    drift that would invalidate the sibling ccache.
    """
    work = os.environ.get("WORK_DIR", "")
    if not work:
        return
    Path(work).mkdir(parents=True, exist_ok=True)
    (Path(work) / "cmake-args.json").write_text(
        json.dumps(list(args), indent=2)
    )


def quick_check_or_continue() -> None:
    """If RECIPE_QUICK_CHECK=1, build LLVMDemangle and exit(0)."""
    if os.environ.get("RECIPE_QUICK_CHECK", "0") != "1":
        return
    ncpus = os.environ.get("NCPUS", "4")
    subprocess.run(["ninja", "-j", ncpus, "LLVMDemangle"], check=True)
    print("build.py: RECIPE_QUICK_CHECK passed (cmake configure + LLVMDemangle).",
          flush=True)
    sys.exit(0)


def cleanup_intermediates() -> None:
    """Drop .o + .obj files under cwd (the build directory).

    ccache state is unaffected (ccache keys on source+flags, not the
    .o on disk). Frees disk before the install phase, which on hosted
    Linux runners with ~14 GiB free historically pushed asan-instrumented
    builds over the limit.
    """
    cwd = Path.cwd()
    _print_disk("pre-install disk", cwd)
    print("build.py: dropping intermediate object files", flush=True)
    count = 0
    for ext in ("*.o", "*.obj"):
        for p in cwd.rglob(ext):
            try:
                p.unlink()
                count += 1
            except OSError:
                pass
    print(f"build.py: removed {count} intermediate object files", flush=True)
    _print_disk("post-cleanup disk", cwd)


def install_distribution(extras: Optional[Sequence[str]] = None) -> None:
    """Compute LLVM_DISTRIBUTION_COMPONENTS from built libs + umbrellas.

    Walks lib/lib*.a + lib/*.lib for the components that exist on disk,
    appends caller-supplied extras (orc_rt platform variants, etc.),
    delegates to run_install_distribution.
    Cwd: build directory.
    """
    dist = [
        "clang", "clang-headers", "clang-cmake-exports",
        "clang-resource-headers", "clangInterpreter",
        "cmake-exports", "llvm-headers", "llvm-config",
        # tablegen binaries: LLVMConfig.cmake's TableGen.cmake exports
        # LLVM_TABLEGEN_EXE pointing at bin/llvm-tblgen, and downstream
        # consumers that re-invoke `tablegen()` (ROOT's bundled cling,
        # any project that ships .td files and wants to find the host
        # tool through the installed LLVM) need the binaries on disk.
        "llvm-tblgen", "clang-tblgen",
    ]
    lib = Path("lib")
    if lib.is_dir():
        for f in sorted(lib.iterdir()):
            name = f.name
            if not (name.endswith(".a") or name.endswith(".lib")):
                continue
            base = name[3:] if name.startswith("lib") else name
            base = base[:-2] if base.endswith(".a") else base[:-4]
            if base.startswith("clang") or base.startswith("LLVM"):
                dist.append(base)
    if extras:
        dist.extend(extras)
    run_install_distribution(";".join(dist))


def run_install_distribution(dist_str: str) -> None:
    """Reconfigure with LLVM_DISTRIBUTION_COMPONENTS, build+install each component.

    For each component X, runs `ninja install-X` — that's a phony
    target depending on (a) X being built, (b) X's install rule. So
    components the recipe ninja'd explicitly are no-op-built; ones
    that weren't (e.g. `llvm-config`, which the recipe doesn't list
    in its ninja line but which the umbrella expects) get built
    before install.

    We deliberately avoid `ninja install-distribution`: that single
    meta-target depends on every library in the configured project
    being built, not just the components in LLVM_DISTRIBUTION_COMPONENTS.
    On a partial build (e.g. llvm-dry-run, which only builds
    LLVMDemangle by hand) install-distribution cascades into all
    ~248 LLVMSupport sources, defeating the dry-run's fast-feedback
    property. Per-component `ninja install-X` walks each component's
    own dep closure — small, no cascade.

    We also avoid plain `cmake --install . --component X`: cmake
    --install runs the install rule with NO build dependency, so any
    component whose file isn't already on disk fails the install
    step. That's the regression class that sent llvm-config missing
    on macOS publishes (`install-llvm-config: file INSTALL cannot
    find … bin/llvm-config: No such file or directory`). Building
    via ninja install-X first guarantees the file exists before
    install runs.

    Cwd: build directory.
    """
    print(f"build.py: LLVM_DISTRIBUTION_COMPONENTS={dist_str}", flush=True)
    subprocess.run(
        ["cmake", f"-DLLVM_DISTRIBUTION_COMPONENTS={dist_str}", "."],
        check=True,
    )
    ncpus = os.environ.get("NCPUS", "4")
    for comp in dist_str.split(";"):
        if not comp:
            continue
        subprocess.run(
            ["ninja", "-j", ncpus, f"install-{comp}"],
            check=True,
        )


def smoke(required_files: Optional[Sequence[str]] = None,
          packages: Sequence[str] = ("LLVM", "Clang")) -> None:
    """find_package() the install tree from a throwaway cmake project.

    Catches missing-.a-in-exports inconsistency (find_package walks
    every IMPORTED target's IMPORTED_LOCATION_RELEASE and validates
    the file exists), plus LLVMConfig load-time issues like the
    LibEdit `check_include_file` C-language try_compile (the smoke
    project enables both C and CXX so that probe works).

    `packages`: which find_package(X REQUIRED) to call. Defaults to
    LLVM + Clang for the asan/root recipes; the dry-run recipe passes
    just ("LLVM",) since clang components aren't installed.

    `required_files`: extra paths under the install prefix that must
    exist (e.g. "lib/libclingInterpreter.a" — cling has no Config.cmake).
    """
    out_dir = os.environ["OUT_DIR"]
    prefix = Path(out_dir) / "llvm-project"
    pkg_calls = "\n".join(
        f'find_package({p} REQUIRED CONFIG PATHS "${{SMOKE_LLVM_PREFIX}}/lib/cmake/{p.lower()}" NO_DEFAULT_PATH)'
        for p in packages
    )
    cmake_lists = f"""\
cmake_minimum_required(VERSION 3.20)
project(install_tree_smoke LANGUAGES C CXX)
{pkg_calls}
message(STATUS "smoke: LLVM ${{LLVM_VERSION_MAJOR}}.${{LLVM_VERSION_MINOR}}.${{LLVM_VERSION_PATCH}} loaded from ${{LLVM_DIR}}")
foreach(rel IN LISTS SMOKE_REQUIRED_FILES)
  if(rel AND NOT EXISTS "${{SMOKE_LLVM_PREFIX}}/${{rel}}")
    message(FATAL_ERROR "smoke: required file missing from install tree: ${{rel}}")
  endif()
endforeach()
"""
    required = ";".join(required_files or [])
    print(
        f"build.py: smoke-testing install tree (find_package "
        f"{', '.join(packages)}{' + existence: ' + required if required else ''})",
        flush=True,
    )
    with tempfile.TemporaryDirectory() as smoke_dir:
        smoke_path = Path(smoke_dir)
        (smoke_path / "CMakeLists.txt").write_text(cmake_lists)
        log_path = smoke_path / "log"
        result = subprocess.run(
            ["cmake", "-S", str(smoke_path), "-B", str(smoke_path / "build"),
             f"-DSMOKE_LLVM_PREFIX={prefix}",
             f"-DSMOKE_REQUIRED_FILES={required}"],
            stdout=log_path.open("wb"), stderr=subprocess.STDOUT,
        )
        if result.returncode != 0:
            print("::error::install tree failed find_package smoke. "
                  "Exports likely reference missing files.", file=sys.stderr)
            try:
                tail = log_path.read_text().splitlines()[-50:]
                sys.stderr.write("\n".join(tail) + "\n")
            except OSError:
                pass
            sys.exit(1)
    print("build.py: smoke passed.", flush=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _print_disk(label: str, path: Path) -> None:
    """Emit a one-line disk-usage summary for `path` if shutil.disk_usage works."""
    import shutil
    try:
        u = shutil.disk_usage(path)
        gib = 1024 ** 3
        print(f"build.py: {label}: free={u.free/gib:.1f} GiB / "
              f"total={u.total/gib:.1f} GiB", flush=True)
    except OSError:
        pass
