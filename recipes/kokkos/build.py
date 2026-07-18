#!/usr/bin/env python3
"""Builds a Kokkos install tree from a tagged release (Serial, Release).

The install ships Kokkos's CMake package config so consumers
find_package(Kokkos) and link Kokkos::kokkos. See recipe.yaml for why this
is a source build and not an apt package.

Serial-only is deliberate: it keeps the build ~1 minute and avoids the
OpenMP backend (Ubuntu's libkokkos-dev is OpenMP-enabled, which forces
every consumer TU onto -fopenmp). Kokkos is built with the consumer's
compiler when CXX is set in the environment -- clad's *-kokkos rows are
all clang, and building Kokkos with the same compiler keeps its recorded
compiler check happy.

Inputs (env): see actions/lib/llvm_build.py docstring for the
RECIPE_VERSION / WORK_DIR / OUT_DIR / NCPUS contract.

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT   sha of the Kokkos tag that was built.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402

KOKKOS_REPO = "https://github.com/kokkos/kokkos.git"


def _compiler_args() -> list[str]:
    """Build Kokkos with the consumer's compiler when the environment names
    one, so Kokkos's installed compiler check matches at find_package time."""
    args = []
    if os.environ.get("CXX"):
        args.append(f"-DCMAKE_CXX_COMPILER={os.environ['CXX']}")
    if os.environ.get("CC"):
        args.append(f"-DCMAKE_C_COMPILER={os.environ['CC']}")
    return args


def _verify_find_package(prefix: Path, ncpus: str) -> None:
    """Post-install smoke: a fresh project can find_package(Kokkos) and call
    the host math (Kokkos::sqrt(double)) the apt packages can't provide.
    Catches the broken-CMake-config failure mode the apt route hits, and a
    compiler mismatch, before the cell is published."""
    smoke = prefix.parent / "smoke"
    smoke.mkdir(exist_ok=True)
    (smoke / "m.cpp").write_text(textwrap.dedent("""\
        #include <Kokkos_Core.hpp>
        double f(double x, double y) {
          return Kokkos::sqrt(x * x + 1.0) * Kokkos::cos(y) + Kokkos::sin(x * y);
        }
        int main() { return 0; }
        """))
    (smoke / "CMakeLists.txt").write_text(textwrap.dedent("""\
        cmake_minimum_required(VERSION 3.20)
        project(smoke CXX)
        find_package(Kokkos REQUIRED)
        add_executable(smoke m.cpp)
        target_link_libraries(smoke Kokkos::kokkos)
        """))
    subprocess.run(
        ["cmake", "-S", str(smoke), "-B", str(smoke / "b"),
         f"-DKokkos_ROOT={prefix}", *_compiler_args()],
        check=True,
    )
    subprocess.run(["cmake", "--build", str(smoke / "b"), "-j", ncpus],
                   check=True)
    print("build.py: find_package(Kokkos) + host-math smoke ok", flush=True)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    src = work_dir / "kokkos"
    llvm_build.clone_shallow(KOKKOS_REPO, version, src)
    llvm_build.record_src_commit(src)

    prefix = out_dir / "install"
    build = work_dir / "build"
    cmake_args = [
        "cmake", "-S", str(src), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DKokkos_ENABLE_SERIAL=ON",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        *_compiler_args(),
    ]
    print("build.py: " + " ".join(cmake_args), flush=True)
    subprocess.run(cmake_args, check=True)
    subprocess.run(
        ["cmake", "--build", str(build), "--target", "install", "-j", ncpus],
        check=True,
    )
    _verify_find_package(prefix, ncpus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
