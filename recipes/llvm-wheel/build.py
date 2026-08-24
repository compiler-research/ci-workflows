#!/usr/bin/env python3
"""Builds the wheel-toolchain LLVM/Clang install tree.

See recipe.yaml for the design rationale. Recipe-specific bits live
here; the shared install-tree publish flow (env validation,
LLVM_DISTRIBUTION_COMPONENTS, install-distribution, find_package
smoke) lives in actions/lib/llvm_build.py.

Inputs (env): see actions/lib/llvm_build.py, plus
  RECIPE_VERSION       exact LLVM release tag suffix ('21.1.8');
                       _source_ref refuses anything else.
  LLVM_WHEEL_INNER     set by the docker wrapper; do not set by hand.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402

# Must match the FROM in docker/manylinux-llvm-wheel/Dockerfile.
_MANYLINUX_IMAGE = "quay.io/pypa/manylinux_2_28_x86_64"
_MACOS_DEPLOYMENT_TARGET = "14.0"


def _source_ref(version: str) -> str:
    """Map the recipe version to the immutable llvmorg release tag."""
    if not re.fullmatch(r"\d+\.\d+\.\d+(-rc\d+)?", version):
        raise ValueError(
            f"llvm-wheel needs an exact release tag suffix "
            f"(e.g. '21.1.8'), not '{version}': a wheel toolchain "
            f"names exactly what it holds")
    return f"llvmorg-{version}"


def _link_jobs() -> str:
    """Cap parallel links: LLVM Release links take gigabytes each, and
    NCPUS-wide linking OOMs 16 GB runners."""
    ncpus = int(os.environ.get("NCPUS", "4"))
    return str(min(4, ncpus))


def _reexec_in_container() -> int:
    """Run this script again inside the manylinux container.

    The bind mounts keep the outer contract intact: the install tree
    lands in the runner's OUT_DIR, and SRC_COMMIT reaches the runner's
    GITHUB_ENV.
    """
    repo_root = (SCRIPT_DIR / ".." / "..").resolve()
    work = os.environ["WORK_DIR"]
    out = os.environ["OUT_DIR"]

    cmd = ["docker", "run", "--rm",
           "-v", f"{repo_root}:/ciwf",
           "-v", f"{work}:/work",
           "-v", f"{out}:/out",
           "-e", "LLVM_WHEEL_INNER=1",
           "-e", "WORK_DIR=/work",
           "-e", "OUT_DIR=/out"]
    for passthrough in ("RECIPE_VERSION", "NCPUS", "RECIPE_ARCH",
                        "RECIPE_OS", "RECIPE_QUICK_CHECK"):
        if os.environ.get(passthrough):
            cmd += ["-e", f"{passthrough}={os.environ[passthrough]}"]
    ccache_dir = os.environ.get("CCACHE_DIR", "")
    if ccache_dir:
        Path(ccache_dir).mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{ccache_dir}:/ccache", "-e", "CCACHE_DIR=/ccache"]
        for launcher in ("CMAKE_C_COMPILER_LAUNCHER",
                         "CMAKE_CXX_COMPILER_LAUNCHER"):
            if os.environ.get(launcher):
                cmd += ["-e", f"{launcher}={os.environ[launcher]}"]
    github_env = os.environ.get("GITHUB_ENV", "")
    if github_env:
        cmd += ["-v", f"{github_env}:/github_env", "-e",
                "GITHUB_ENV=/github_env"]

    # The image ships several CPythons under /opt/python and a pipx
    # cmake, but no ninja; pip-installing both into one interpreter
    # keeps the build independent of the image's floating tool set.
    # ccache is also absent from the image, so the launcher is dropped
    # and linux cells build cold — acceptable for a rarely-bumped
    # toolchain.
    inner = (
        "py=$(ls -d /opt/python/cp3*/bin/python | head -1); "
        "$py -m pip install -q cmake ninja; "
        "export PATH=$(dirname $py):$PATH; "
        "command -v ccache >/dev/null || "
        "unset CMAKE_C_COMPILER_LAUNCHER CMAKE_CXX_COMPILER_LAUNCHER; "
        "exec $py /ciwf/recipes/llvm-wheel/build.py"
    )
    cmd += [_MANYLINUX_IMAGE, "bash", "-c", inner]
    print(f"build.py: re-executing in {_MANYLINUX_IMAGE}", flush=True)
    return subprocess.run(cmd, check=False).returncode


def _static_only_or_die(prefix: Path) -> None:
    """The wheel contract: no LLVM/clang dylibs may ship."""
    stray = [p for pat in ("libLLVM*.so*", "libLLVM*.dylib",
                           "libclang*.so*", "libclang*.dylib")
             for p in (prefix / "lib").glob(pat)]
    if stray:
        raise RuntimeError(f"static-only install violated: {stray}")


def main() -> int:
    llvm_build.setup_env()
    version = os.environ["RECIPE_VERSION"]
    try:
        src_ref = _source_ref(version)
    except ValueError as err:
        print(f"::error::{err}", file=sys.stderr)
        return 1

    if sys.platform.startswith("linux") and \
            not os.environ.get("LLVM_WHEEL_INNER"):
        if os.environ.get("RECIPE_ARCH", "x86_64") != "x86_64":
            print("::error::llvm-wheel linux cells are x86_64-only "
                  "(the recipe wires only the x86_64 manylinux image)",
                  file=sys.stderr)
            return 1
        if not shutil.which("docker"):
            print("::error::llvm-wheel linux builds need docker "
                  "(the build runs inside the manylinux container)",
                  file=sys.stderr)
            return 1
        return _reexec_in_container()

    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    ncpus = os.environ["NCPUS"]

    os.chdir(work_dir)
    print(f"build.py: version={version}; cloning {src_ref}", flush=True)
    llvm_build.clone_shallow(
        "https://github.com/llvm/llvm-project.git",
        src_ref,
        work_dir / "llvm-project",
    )
    llvm_build.record_src_commit(work_dir / "llvm-project")

    build_dir = work_dir / "llvm-project" / "build"
    build_dir.mkdir(exist_ok=True)
    os.chdir(build_dir)

    # Deliberately NOT base_cmake_args(): assertions and the dylibs
    # diverge, and silently inheriting a flag bump meant for the CI
    # toolchains would change what ships inside user wheels.
    cmake_args = [
        "cmake", "-G", "Ninja",
        f"-DCMAKE_INSTALL_PREFIX={out_dir / 'install'}",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLVM_ENABLE_ASSERTIONS=OFF",
        "-DLLVM_TARGETS_TO_BUILD=host",
        "-DLLVM_ENABLE_PROJECTS=clang;lld",
        "-DLLVM_ENABLE_RTTI=ON",
        "-DLLVM_ENABLE_ZLIB=FORCE_ON",
        "-DLLVM_ENABLE_ZSTD=OFF",
        "-DLLVM_ENABLE_LIBXML2=OFF",
        "-DLLVM_ENABLE_LIBEDIT=OFF",
        f"-DLLVM_PARALLEL_LINK_JOBS={_link_jobs()}",
        "-DCLANG_ENABLE_STATIC_ANALYZER=OFF",
        "-DCLANG_ENABLE_ARCMT=OFF",
        "-DCLANG_ENABLE_FORMAT=OFF",
        "-DCLANG_ENABLE_BOOTSTRAP=OFF",
        "-DLLVM_INCLUDE_BENCHMARKS=OFF",
        "-DLLVM_INCLUDE_EXAMPLES=OFF",
        "-DLLVM_INCLUDE_TESTS=OFF",
    ]
    if sys.platform == "darwin":
        # The floor every produced object carries; delocate and the
        # consumer's wheel tag verify against it downstream.
        cmake_args.append(
            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={_MACOS_DEPLOYMENT_TARGET}")
    cmake_args += llvm_build.cmake_extra()
    cmake_args += ["../llvm"]
    llvm_build.record_cmake_args(cmake_args)
    subprocess.run(cmake_args, check=True)

    llvm_build.quick_check_or_continue()

    # The same library surface llvm-release builds for CppInterOp
    # consumers, plus the lld binary.
    subprocess.run(
        ["ninja", "-j", ncpus,
         "clang", "clangInterpreter", "clangStaticAnalyzerCore", "lld"],
        check=True,
    )

    llvm_build.cleanup_intermediates()
    llvm_build.install_distribution(extras=["lld"])

    _static_only_or_die(out_dir / "install")
    major = version.split(".")[0]
    # install-lld ships the platform driver symlinks; assert the one a
    # consumer's -fuse-ld=lld resolves.
    driver = "ld64.lld" if sys.platform == "darwin" else "ld.lld"
    llvm_build.smoke(required_files=[
        f"lib/clang/{major}/include/stddef.h",
        "bin/lld",
        f"bin/{driver}",
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
