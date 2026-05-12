#!/usr/bin/env python3
"""Builds an asan+ubsan-instrumented CPython install tree.

Configure flags drive the diagnostic surface:
  --with-address-sanitizer         -fsanitize=address through
                                   BASECFLAGS / PY_LDFLAGS; every
                                   stdlib C extension built by
                                   Modules/Setup inherits the flag.
  --with-undefined-behavior-sanitizer  -fsanitize=undefined likewise.
  --enable-shared                  consumers link libpython3.<N>.so.
  --disable-test-modules           in-tree test extensions are large
                                   and unused; drops a few hundred MB.

System clang from install-build-deps drives the build. Downstream
consumers must match compiler family (gcc-asan vs clang-asan have
non-unifying runtimes); see recipe.yaml for the full caveat. No
`bootstrap:` declared -- standalone, identical scaffold to cpython-debug.

Inputs (env): see actions/lib/llvm_build.py docstring for the
RECIPE_VERSION / WORK_DIR / OUT_DIR / NCPUS contract.

Outputs (env, written to GITHUB_ENV when present):
  SRC_COMMIT             sha of cpython HEAD that was built.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / ".." / ".." / "actions" / "lib"))

import llvm_build  # noqa: E402


CPYTHON_REPO = "https://github.com/python/cpython.git"


def _verify_asan_build(python_bin: Path, install_prefix: Path) -> None:
    """Post-install assertion: asan + ubsan are genuinely wired in.

    Catches the silent-regression failure mode where the configure
    flags get removed but the cell still ships under the asan name.
    `__asan_init` resolves in the main namespace iff the interpreter
    was linked against the asan runtime; UBSan has no comparable
    always-resident symbol, so we fall back to sysconfig.

    LD_LIBRARY_PATH points at $prefix/lib so the launcher resolves
    libpython3.<N>.so (off the default loader search path with
    --enable-shared). ASAN_OPTIONS=detect_leaks=0 keeps the verify
    from exiting 23 on Python's intentional interning at shutdown.
    """
    env = {
        **os.environ,
        "LD_LIBRARY_PATH": str(install_prefix / "lib"),
        "ASAN_OPTIONS": "detect_leaks=0",
    }
    out = subprocess.run(
        [str(python_bin), "-c",
         "import ctypes, sys, sysconfig; "
         "assert hasattr(ctypes.CDLL(None), '__asan_init'), "
         "'cell built without --with-address-sanitizer'; "
         "cfl = sysconfig.get_config_var('PY_CFLAGS') or ''; "
         "assert '-fsanitize=undefined' in cfl, "
         "'cell built without --with-undefined-behavior-sanitizer'; "
         "print(sys.version)"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    print(f"build.py: asan+ubsan verify ok: {out}", flush=True)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    src_dir = work_dir / "cpython"
    install_prefix = out_dir / "install"

    os.chdir(work_dir)
    llvm_build.clone_shallow(CPYTHON_REPO, f"v{version}", src_dir)
    src_commit = llvm_build.record_src_commit(src_dir)

    install_prefix.parent.mkdir(parents=True, exist_ok=True)
    if install_prefix.exists():
        # configure / make install are idempotent but `--prefix=...`
        # writes by overlay, leaving stale files from a prior run.
        # Wipe the prefix so the cell content matches exactly what
        # THIS build produced.
        shutil.rmtree(install_prefix)

    print(f"build.py: configuring asan+ubsan CPython {version} -> "
          f"{install_prefix}", flush=True)
    subprocess.run(
        ["./configure",
         f"--prefix={install_prefix}",
         "--with-address-sanitizer",
         "--with-undefined-behavior-sanitizer",
         "--enable-shared",
         "--disable-test-modules"],
        cwd=src_dir, check=True,
    )

    print(f"build.py: building (-j{ncpus})", flush=True)
    subprocess.run(["make", "-j", ncpus], cwd=src_dir, check=True)

    print("build.py: installing", flush=True)
    subprocess.run(["make", "install"], cwd=src_dir, check=True)

    major_minor = ".".join(version.split(".")[:2])
    python_bin = install_prefix / "bin" / f"python{major_minor}"
    _verify_asan_build(python_bin, install_prefix)

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
