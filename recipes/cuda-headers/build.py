#!/usr/bin/env python3
"""Assembles a headers-only CUDA prefix from NVIDIA's redistributables.

See recipe.yaml for what is included and why.

Inputs (env): RECIPE_VERSION / RECIPE_ARCH / WORK_DIR / OUT_DIR, per the
contract in actions/lib/llvm_build.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REDIST = "https://developer.download.nvidia.com/compute/cuda/redist"

# cudart, nvcc and curand between them hold everything clang's force-included
# __clang_cuda_runtime_wrapper.h reaches; cccl adds Thrust/CUB/libcu++.
COMPONENTS = ("cuda_cudart", "cuda_nvcc", "cuda_cccl", "libcurand")

# NVIDIA's platform keys, which are not the runner-image slugs. Linux only;
# main() refuses a cell for anything else.
ARCH_KEY = {"x86_64": "linux-x86_64", "arm64": "linux-sbsa"}


def fetch(url: str, dest: Path, sha256: str | None = None) -> None:
    """Downloads to dest, verifying against the manifest's digest."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as r, dest.open("wb") as f:
        while chunk := r.read(1 << 20):
            digest.update(chunk)
            f.write(chunk)
    if sha256 and digest.hexdigest() != sha256:
        raise RuntimeError(
            f"{dest.name}: sha256 {digest.hexdigest()} != manifest {sha256}")


def copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def main() -> int:
    version = os.environ["RECIPE_VERSION"]
    work = Path(os.environ["WORK_DIR"])
    # setup-recipe moves exactly $OUT_DIR/install and ignores anything else.
    out = Path(os.environ["OUT_DIR"]) / "install"

    # The arch must be the one the cache key was computed from, not the
    # host's -- guessing would file one architecture's headers under
    # another's key.
    arch = os.environ.get("RECIPE_ARCH")
    if not arch:
        print("error: RECIPE_ARCH is unset", file=sys.stderr)
        return 1
    plat = ARCH_KEY.get(arch)
    if plat is None:
        print(f"error: unsupported arch {arch!r}", file=sys.stderr)
        return 1

    # Only Linux components are mapped above, and NVIDIA ships Windows ones
    # too, so a cell for another OS would otherwise be served Linux headers
    # without complaint.
    os_slug = os.environ.get("RECIPE_OS", "")
    if os_slug and not os_slug.startswith("ubuntu"):
        print(f"error: {os_slug} is not supported; this recipe is Linux only",
              file=sys.stderr)
        return 1
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / f"redistrib_{version}.json"
    fetch(f"{REDIST}/redistrib_{version}.json", manifest_path)
    manifest = json.loads(manifest_path.read_text())

    out.joinpath("include").mkdir(parents=True, exist_ok=True)
    # clang's probe rejects the prefix outright without bin/, but never looks
    # inside it for a host-only parse.
    out.joinpath("bin").mkdir(parents=True, exist_ok=True)

    for name in COMPONENTS:
        comp = manifest.get(name)
        if comp is None or plat not in comp:
            print(f"error: {name} has no {plat} in {version}", file=sys.stderr)
            return 1
        entry = comp[plat]
        rel = entry["relative_path"]
        print(f"{name} {comp['version']}: {rel}", flush=True)
        tarball = work / Path(rel).name
        fetch(f"{REDIST}/{rel}", tarball, entry.get("sha256"))
        with tarfile.open(tarball) as tf:
            # Pinned: the default differs across Python versions.
            tf.extractall(work, filter="data")
        root = work / Path(rel).name.replace(".tar.xz", "")

        if (root / "include").is_dir():
            copy_tree(root / "include", out / "include")
        # The one non-header the probe checks for. Never read by a host-only
        # parse.
        if (root / "nvvm" / "libdevice").is_dir():
            copy_tree(root / "nvvm" / "libdevice", out / "nvvm" / "libdevice")

    # clang itself reads CUDA_VERSION from cuda.h; this is for tooling that
    # expects the file.
    (out / "version.txt").write_text(f"CUDA Version {version}\n")

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"cuda-headers {version} ({arch}): {total / 1e6:.1f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
