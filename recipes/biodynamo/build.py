#!/usr/bin/env python3
"""Builds a relocatable BioDynaMo install tree.

BioDynaMo installs into <prefix>/biodynamo-v<short-version>/ and pulls its own
prebuilt ROOT into third_party/root, so the flattened result is self-contained:
bin/thisbdm.sh derives BDMSYS from its own location and everything else hangs
off that. See recipe.yaml for why python3.9, g++-11 and -DNOPYENV=YES are all
load-bearing.

Inputs (env): see actions/lib/llvm_build.py for the
RECIPE_VERSION / WORK_DIR / OUT_DIR / NCPUS contract.

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT   sha of the BioDynaMo tag that was built.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402

BDM_REPO = "https://github.com/vgvassilev/biodynamo.git"

# BioDynaMo's own prerequisites, matching what its consumers' CI installs.
APT_PACKAGES = [
    "cmake", "make", "g++", "gcc", "git", "wget", "curl",
    "libblas-dev", "liblapack-dev", "libnuma-dev", "libomp-dev",
    "libopenmpi-dev", "libpthread-stubs0-dev", "zlib1g-dev",
    "freeglut3-dev", "libbz2-dev", "libffi-dev", "liblzma-dev",
    "libreadline-dev", "libsqlite3-dev", "libssl-dev", "tk-dev",
    "xz-utils",
    # Without it BioDynaMo's -Dlibgit2=ON silently degrades to
    # "Libgit2 not found. GitTracking will not be available."
    "libgit2-dev",
    # See recipe.yaml: cling needs the compiler ROOT was built with.
    "g++-11",
]


def _sudo() -> list[str]:
    # geteuid is POSIX-only. This recipe has Linux cells exclusively (the ROOT
    # tarballs BioDynaMo fetches are Linux/macOS), but bin/recipe-cache build
    # can be invoked anywhere, so fail with a reason rather than an
    # AttributeError from os.
    if not hasattr(os, "geteuid"):
        raise RuntimeError("recipes/biodynamo builds on POSIX hosts only")
    return [] if os.geteuid() == 0 else ["sudo"]


#: Prefixed to every apt invocation via `env`, not passed through
#: subprocess(env=...): these run under sudo, which resets the environment, so
#: an inherited variable never reaches apt.
#:
#: NEEDRESTART_MODE=l tells needrestart to list services needing a restart
#: rather than restarting them. Left at its default it bounces
#: systemd-networkd and systemd-resolved partway through the job when a library
#: upgrade pulls them in -- restarting the runner's network stack underneath a
#: build that then fetches a few hundred MB of ROOT.
_APT_ENV = ("env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l")


def _apt(*packages: str) -> None:
    subprocess.run([*_sudo(), *_APT_ENV, "apt-get", "install", "-y",
                    "--no-install-recommends", *packages], check=True)


def _install_deps() -> None:
    """Host packages BioDynaMo's configure and ROOT's cling both need."""
    subprocess.run([*_sudo(), *_APT_ENV, "apt-get", "update", "-qq"],
                   check=True)
    _apt(*APT_PACKAGES)
    # python3.9 is not in any Ubuntu LTS; deadsnakes is the only apt source.
    # Skipped when a 3.9 is already present so a re-run stays cheap.
    if shutil.which("python3.9") is None:
        _apt("software-properties-common")
        subprocess.run([*_sudo(), *_APT_ENV, "add-apt-repository", "-y",
                        "ppa:deadsnakes/ppa"], check=True)
        subprocess.run([*_sudo(), *_APT_ENV, "apt-get", "update", "-qq"],
                       check=True)
    _apt("python3.9", "python3.9-dev")


def _flatten_install(staging: Path, dest: Path) -> Path:
    """Move <staging>/biodynamo-v<X>/ to `dest`.

    setup-recipe expects the tarball root to be `install/`, while BioDynaMo
    installs one level deeper under a version-stamped directory name.
    """
    entries = [p for p in staging.iterdir() if p.is_dir()]
    if len(entries) != 1:
        raise RuntimeError(
            f"expected exactly one install dir under {staging}, got {entries}"
        )
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(entries[0]), str(dest))
    shutil.rmtree(staging, ignore_errors=True)
    return dest


def _record_root_provenance(prefix: Path) -> None:
    """Stamp the bundled ROOT's version and Python ABI into the install tree.

    ROOT supplies the random number generator BioDynaMo simulations draw from,
    so which ROOT went into this artifact is a scientific input and not just a
    build detail. The manifest has no field for it, so it travels with the
    artifact instead.
    """
    root_config = prefix / "third_party" / "root" / "bin" / "root-config"
    if not root_config.is_file():
        print("build.py: no bundled root-config; skipping provenance stamp",
              flush=True)
        return

    def _ask(flag: str) -> str:
        r = subprocess.run([str(root_config), flag], capture_output=True,
                           text=True)
        return r.stdout.strip() if r.returncode == 0 else "unknown"

    size_mib = sum(p.stat().st_size for p in prefix.rglob("*") if p.is_file())
    size_mib = round(size_mib / 1048576)
    provenance = {
        "root_version": _ask("--version"),
        "root_python_version": _ask("--python-version"),
        # Measured rather than asserted: this artifact counts against
        # cells.yaml's caps, and a stale figure in a comment is how a cache
        # quietly outgrows them.
        "install_size_mib": size_mib,
    }
    out = prefix / "share" / "bdm-recipe-provenance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"build.py: bundled ROOT {provenance['root_version']} "
          f"(python {provenance['root_python_version']}), "
          f"install {size_mib} MiB", flush=True)


def _verify_relocated(prefix: Path, work_dir: Path, ncpus: str) -> None:
    """Post-install smoke against a *moved* copy of the install tree.

    Relocation is the failure mode that matters here: the consumer extracts to
    a different absolute path than the producer built at, and ROOT bakes paths
    into its PCH and dictionaries. Building at the original location proves
    nothing, so drive the whole consumer path from a different one -- source
    thisbdm.sh, configure and build BioDynaMo's own simulation-template, run
    it, and import cppyy.

    The tree is *moved* rather than copied, then moved back in `finally`. A
    copy would double peak disk on a GB-scale artifact, and leaving the
    original in place lets a stale absolute path resolve to it and pass a test
    that would fail on a real consumer.
    """
    moved = work_dir / "relocated" / "install"
    if moved.parent.exists():
        shutil.rmtree(moved.parent)
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(prefix), str(moved))
    try:
        _run_relocated_smoke(moved, work_dir, ncpus)
    finally:
        shutil.move(str(moved), str(prefix))
        shutil.rmtree(moved.parent, ignore_errors=True)
    print("build.py: relocated install smoke ok", flush=True)


def _run_relocated_smoke(moved: Path, work_dir: Path, ncpus: str) -> None:
    """Drive a consumer build against the install tree at `moved`."""
    template = moved / "simulation-template"
    if not template.is_dir():
        raise RuntimeError(f"no simulation-template in install tree: {moved}")
    sim = work_dir / "relocated" / "sim"
    if sim.exists():
        shutil.rmtree(sim)
    shutil.copytree(template, sim, symlinks=True)

    # thisbdm.sh reports failure on stderr but still exits 0, so assert on
    # BDMSYS rather than on the exit status.
    #
    # The cppyy check runs under the interpreter ROOT's bindings were built
    # for, not whatever `python3` resolves to: with -DNOPYENV=YES nothing puts
    # that version first on PATH, and the system python is 3.12 on ubuntu-24.04
    # while the bundled ROOT ships libcppyy3_9. Ask ROOT which one it wants,
    # the same way BioDynaMo's own configure does.
    script = textwrap.dedent(f"""
        set -e
        source "{moved}/bin/thisbdm.sh"
        if [ -z "${{BDMSYS:-}}" ]; then
          echo "thisbdm.sh did not set BDMSYS" >&2
          exit 1
        fi
        echo "smoke: BDMSYS=$BDMSYS"
        # Same compiler pin as the BioDynaMo build above, and for the same
        # reason: this is a second cmake invocation, so it re-detects from the
        # ambient CC/CXX (clang, in the publish workflow) while UseBioDynaMo
        # swaps in mpicxx, which wraps gcc. Mismatched OpenMP flags then break
        # the consumer build rather than BioDynaMo's own.
        cmake -S "{sim}" -B "{sim}/build" -DCMAKE_BUILD_TYPE=Release \\
              -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++
        cmake --build "{sim}/build" -j {ncpus}
        # Running it is the point: ROOT bakes absolute paths into its PCH and
        # rootmap, and a relocated tree that compiles happily still fails at
        # load. Only executing the binary exercises that.
        cd "{sim}/build" && ./my-simulation
        pyver=$("$BDMSYS/third_party/root/bin/root-config" --python-version \\
                | cut -d. -f1,2)
        "python${{pyver}}" -c 'import cppyy; print("smoke: import cppyy ok")'
    """)
    subprocess.run(["bash", "-c", script], check=True)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    _install_deps()

    src = work_dir / "biodynamo"
    llvm_build.clone_shallow(BDM_REPO, version, src)
    llvm_build.record_src_commit(src)

    staging = work_dir / "install-staging"
    build = work_dir / "build"
    cmake_args = [
        "cmake", "-S", str(src), "-B", str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        # See recipe.yaml: keeps pyenv out of the generated thisbdm.sh so the
        # published tree is sourceable on a consumer that has no pyenv.
        "-DNOPYENV=YES",
        # A published artifact has to run wherever it is extracted, and a host
        # with no NUMA topology -- any container on Docker for Mac or Windows,
        # WSL, some CI runners -- reports zero nodes while claiming NUMA is
        # available. A NUMA-aware build then allocates on a node that does not
        # exist and dies inside Simulation's constructor. BioDynaMo's non-NUMA
        # path is a thin shim (numa_alloc_onnode -> malloc, one node), and on
        # the single-socket machines this cell targets a real NUMA build does
        # the same thing anyway. Revisit if a consumer runs multi-socket, where
        # the awareness actually pays.
        "-Dnuma=OFF",
        "-Dnotebooks=OFF",
        "-Dparaview=OFF",
        "-Dlibgit2=ON",
        "-Dsbml=OFF",
        f"-DCMAKE_INSTALL_PREFIX={staging}",
        *llvm_build.cmake_extra(),
        # Last, so nothing above can put it back: build with gcc whatever CC/CXX
        # the workflow exported. install-build-deps sets clang for the LLVM
        # recipes, but BioDynaMo replaces CMAKE_CXX_COMPILER with MPI's wrapper
        # regardless, and mpicxx wraps gcc. The replacement happens *after* CMake
        # has detected the compiler, so the flags stay clang-shaped --
        # find_package(OpenMP) yields -fopenmp=libomp, g++ rejects it, and the
        # build dies on the first agent. Pinning detection to what mpicxx
        # actually runs keeps the two consistent, and matches the bundled ROOT,
        # which is a gcc build.
        "-DCMAKE_C_COMPILER=gcc",
        "-DCMAKE_CXX_COMPILER=g++",
    ]
    # Persist for the manifest: repro-config replays this to configure a
    # devshell against the same flags the producer used.
    llvm_build.record_cmake_args(cmake_args)

    print("build.py: " + " ".join(cmake_args), flush=True)
    subprocess.run(cmake_args, check=True)
    subprocess.run(["cmake", "--build", str(build), "-j", ncpus], check=True)
    subprocess.run(["cmake", "--install", str(build)], check=True)

    prefix = _flatten_install(staging, out_dir / "install")
    _record_root_provenance(prefix)
    _verify_relocated(prefix, work_dir, ncpus)
    return 0


if __name__ == "__main__":
    sys.exit(main())
