#!/usr/bin/env python3
"""Builds a debug-mode CPython install tree.

Configure flags drive the diagnostic surface:
  --with-pydebug      enables Py_DEBUG. Refcount tracing
                      (sys.gettotalrefcount), heavy assertions in
                      every type slot, and the PYMEM_DEBUG allocator's
                      0xCB / 0xFB / 0xDB poison-pattern sentinels
                      that catch use-after-free, double-free, and
                      write-past-end on every allocation.
  --with-trace-refs   per-object death-row -- sys.getobjects() walks
                      every alive PyObject; finds dangling refs after
                      Py_DECREF.
  --with-assertions   plain `assert()`s in the C code (independent of
                      Py_DEBUG; cheap to keep on).
  --enable-shared     consumers link libpython3.<N>.so; matches the
                      release-cpython ABI shape so consumer cmake
                      rules port without surgery.
  --disable-test-modules  the in-tree test extensions are large and
                      we don't ship them. Drops a few hundred MB.

System clang from install-build-deps is fine -- debug mode does not
need a sanitised stdlib or a major-matched compiler. No `bootstrap:`
declared in recipe.yaml; publish-recipe's fetch_bootstrap step is a
silent no-op.

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


def _verify_debug_build(python_bin: Path, install_prefix: Path) -> None:
    """Post-install assertion: this is genuinely a debug interpreter.

    Catches the silent-regression failure mode where someone removes
    --with-pydebug from configure and the cell ships a release Python
    under a debug name. sys.gettotalrefcount only exists when Py_DEBUG
    was defined at compile time.

    LD_LIBRARY_PATH: --enable-shared puts libpython3.<N>d.so under
    $prefix/lib, off the default loader search path. The binary itself
    exits 127 ("dynamic linker can't resolve") without it.
    """
    env = {**os.environ, "LD_LIBRARY_PATH": str(install_prefix / "lib")}
    out = subprocess.run(
        [str(python_bin), "-c",
         "import sys; "
         "assert hasattr(sys, 'gettotalrefcount'), "
         "'cell built without --with-pydebug'; "
         "print(sys.version)"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    print(f"build.py: debug verify ok: {out}", flush=True)


def main() -> int:
    llvm_build.setup_env()
    work_dir = Path(os.environ["WORK_DIR"])
    out_dir = Path(os.environ["OUT_DIR"])
    version = os.environ["RECIPE_VERSION"]
    ncpus = os.environ["NCPUS"]

    src_dir = work_dir / "cpython"
    install_prefix = out_dir / "cpython"

    os.chdir(work_dir)
    llvm_build.clone_shallow(CPYTHON_REPO, f"v{version}", src_dir)
    src_commit = llvm_build.record_src_commit(src_dir)

    install_prefix.parent.mkdir(parents=True, exist_ok=True)
    if install_prefix.exists():
        # configure / make install are idempotent but `--prefix=...`
        # writes the install tree by overlay, leaving stale files from
        # a prior run. Wipe the prefix so the cell content matches
        # exactly what THIS build produced.
        shutil.rmtree(install_prefix)

    print(f"build.py: configuring debug CPython {version} -> "
          f"{install_prefix}", flush=True)
    subprocess.run(
        ["./configure",
         f"--prefix={install_prefix}",
         "--with-pydebug",
         "--with-trace-refs",
         "--with-assertions",
         "--enable-shared",
         "--disable-test-modules"],
        cwd=src_dir, check=True,
    )

    print(f"build.py: building (-j{ncpus})", flush=True)
    subprocess.run(["make", "-j", ncpus], cwd=src_dir, check=True)

    print("build.py: installing", flush=True)
    subprocess.run(["make", "install"], cwd=src_dir, check=True)

    # --enable-shared installs the launcher as python<major>.<minor>
    # (no abiflags suffix on the executable itself, even for debug
    # builds; the 'd' tag appears on libpython.so, on extension modules
    # via the SOABI tag, and on the *-config script).
    major_minor = ".".join(version.split(".")[:2])
    python_bin = install_prefix / "bin" / f"python{major_minor}"
    _verify_debug_build(python_bin, install_prefix)

    print(f"build.py: done. SRC_COMMIT={src_commit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
