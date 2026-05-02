"""Scheme-aware probe / download / upload primitives for the recipe cache.

Python port of actions/lib/cache-io.sh. Stdlib-only; shells out to
external tools (zstd, gh) where appropriate. Runs natively on
Linux / macOS / Windows without git-bash.

Supported schemes:
  file:///abs/path  - local directory acting as the cache backend
                      (developer machines, act runs, NFS mounts).
  https://...       - remote URL; reads use urllib. Writes only when
                      the URL points at a github.com Releases asset
                      path, in which case `gh release upload` is used.
  http://...        - same as https://, for lab webservers.

Functions assume the caller has already exported an OS-appropriate
zstd, tar, and (for github uploads) gh.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


_DEFAULT_BASE = "https://github.com/compiler-research/ci-workflows/releases/download/cache/"


def resolve_cache_base(explicit: Optional[str] = None) -> str:
    """Return the effective cache base URL.

    Precedence: explicit arg > RECIPE_CACHE_BASE env > baked-in default.
    """
    if explicit:
        return explicit
    env = os.environ.get("RECIPE_CACHE_BASE", "")
    if env:
        return env
    return _DEFAULT_BASE


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def cache_probe(base: str, key: str) -> bool:
    """Return True if the asset for `key` exists at `base`."""
    base = _strip_trailing_slash(base)
    asset = f"{key}.tar.zst"

    if base.startswith("file://"):
        return (Path(base[len("file://"):]) / asset).is_file()

    if base.startswith(("https://", "http://")):
        url = f"{base}/{asset}"
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError:
            return False
        except urllib.error.URLError:
            return False

    raise ValueError(f"cache_probe: unsupported scheme: {base}")


def cache_download(base: str, key: str, out_dir: str) -> None:
    """Fetch the asset for `key` and extract into `out_dir`.

    The recipe's tarball root (e.g. ``llvm-project/``) lands directly
    under `out_dir`.
    """
    base = _strip_trailing_slash(base)
    asset = f"{key}.tar.zst"
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if base.startswith("file://"):
        src = Path(base[len("file://"):]) / asset
        with src.open("rb") as f:
            _zstd_decode_into_tar(f, out_path)
        return

    if base.startswith(("https://", "http://")):
        url = f"{base}/{asset}"
        with urllib.request.urlopen(url, timeout=300) as resp:
            _zstd_decode_into_tar(resp, out_path)
        return

    raise ValueError(f"cache_download: unsupported scheme: {base}")


def gh_release_url_parse(base: str) -> Optional[Tuple[str, str]]:
    """Parse `https://github.com/OWNER/REPO/releases/download/TAG/`.

    Returns (owner_repo, tag) or None if the URL doesn't match.
    Factored out so callers (and unit tests) can validate URL shape
    without invoking the upload path.
    """
    base = _strip_trailing_slash(base)
    prefix = "https://github.com/"
    marker = "/releases/download/"
    if not base.startswith(prefix) or marker not in base:
        return None
    rest = base[len(prefix):]
    owner_repo = rest.split("/releases/", 1)[0]
    tag = rest.split(marker, 1)[1]
    if not owner_repo or not tag:
        return None
    return owner_repo, tag


def cache_upload(base: str, key: str, asset: str,
                 manifest: Optional[str] = None) -> None:
    """Store the asset (and optional manifest) at the cache backend.

    file://         - cp into the directory (creates if missing).
    https://github.com/.../releases/download/TAG/  - gh release upload.
    anything else   - error (read-only backend).

    Files are stored under their own basename, so callers are
    responsible for naming the asset correctly (e.g. ``<key>.tar.zst``
    for the install tree, ``<key>.ccache.tar.zst`` for a sibling
    ccache snapshot). `manifest` is optional: pass None when uploading
    a sibling asset that reuses the install tree's manifest.
    """
    base = _strip_trailing_slash(base)
    asset_path = Path(asset)
    manifest_path = Path(manifest) if manifest else None

    if base.startswith("file://"):
        dest_dir = Path(base[len("file://"):])
        dest_dir.mkdir(parents=True, exist_ok=True)
        _copy(asset_path, dest_dir / asset_path.name)
        if manifest_path is not None:
            _copy(manifest_path, dest_dir / manifest_path.name)
        return

    parsed = gh_release_url_parse(base)
    if parsed is None:
        raise ValueError(
            f"cache_upload: only file:// or github.com Releases backends "
            f"support writes; got: {base}"
        )
    owner_repo, tag = parsed
    cmd = ["gh", "release", "upload", tag, str(asset_path)]
    if manifest_path is not None:
        cmd.append(str(manifest_path))
    cmd.extend(["-R", owner_repo, "--clobber"])
    subprocess.run(cmd, check=True)


def cache_pack(in_dir: str, key: str, out_dir: Optional[str] = None,
               *, src_name: str = "llvm-project",
               key_suffix: str = "") -> None:
    """Tar+zstd ``in_dir/<src_name>`` to ``out_dir/<key><key_suffix>.tar.zst``.

    Defaults match the install tree (src_name="llvm-project",
    key_suffix=""). For a sibling ccache snapshot pass src_name=".ccache",
    key_suffix=".ccache".

    Uses Python's tarfile module to produce the archive (POSIX format,
    same as GNU/BSD tar can read) and pipes through the zstd binary
    for compression. No tar binary needed — sidesteps GNU vs BSD tar
    flag drift entirely.

    `out_dir` defaults to the current working directory.
    """
    out_dir_path = Path(out_dir) if out_dir else Path.cwd()
    out_name = f"{key}{key_suffix}.tar.zst"
    out_path = out_dir_path / out_name
    src = Path(in_dir) / src_name
    if not src.is_dir():
        raise FileNotFoundError(f"cache_pack: {src} does not exist")

    print(f"::notice::compressing {out_name} (zstd -19 --long -T0)",
          flush=True)

    with out_path.open("wb") as out_f:
        zstd = subprocess.Popen(
            ["zstd", "-19", "--long", "-T0"],
            stdin=subprocess.PIPE, stdout=out_f,
        )
        try:
            with tarfile.open(fileobj=zstd.stdin, mode="w|") as tar:
                tar.add(src, arcname=src_name)
        finally:
            assert zstd.stdin is not None
            zstd.stdin.close()
        rc = zstd.wait()
    if rc != 0:
        raise RuntimeError(f"zstd exited with code {rc}")


# --- internals -----------------------------------------------------------

def _zstd_decode_into_tar(src_fileobj, out_dir: Path) -> None:
    """Stream `src_fileobj` through `zstd -d` into a tar extraction at out_dir."""
    import threading

    zstd = subprocess.Popen(
        ["zstd", "-d", "-c"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert zstd.stdin is not None and zstd.stdout is not None

    # Pump source into zstd stdin in a background thread so tarfile can
    # consume zstd stdout without deadlock.
    def pump():
        try:
            while True:
                chunk = src_fileobj.read(64 * 1024)
                if not chunk:
                    break
                zstd.stdin.write(chunk)
        finally:
            zstd.stdin.close()
    t = threading.Thread(target=pump, daemon=True)
    t.start()

    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as tar:
            tar.extractall(out_dir)
    finally:
        t.join()
        zstd.stdout.close()
        rc = zstd.wait()
    if rc != 0:
        raise RuntimeError(f"zstd -d exited with code {rc}")


def _copy(src: Path, dst: Path) -> None:
    import shutil
    shutil.copyfile(src, dst)


# --- CLI dispatch --------------------------------------------------------
# Action.yml shell steps invoke `python3 -m actions.lib.cache_io <op> ...`
# rather than re-implement the bash sourcing pattern. Each subcommand
# maps to one of the public functions.

def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: cache_io <op> [args...]", file=sys.stderr)
        return 2
    op, rest = argv[0], argv[1:]
    if op == "resolve-base":
        explicit = rest[0] if rest else ""
        print(resolve_cache_base(explicit or None))
        return 0
    if op == "probe":
        base, key = rest
        return 0 if cache_probe(base, key) else 1
    if op == "download":
        base, key, out_dir = rest
        cache_download(base, key, out_dir)
        return 0
    if op == "upload":
        # upload BASE KEY ASSET [MANIFEST]
        if len(rest) == 3:
            base, key, asset = rest
            cache_upload(base, key, asset)
        else:
            base, key, asset, manifest = rest
            cache_upload(base, key, asset, manifest)
        return 0
    if op == "pack":
        # pack IN_DIR KEY [OUT_DIR [SRC_NAME [KEY_SUFFIX]]]
        in_dir, key = rest[:2]
        out_dir = rest[2] if len(rest) > 2 else None
        kwargs = {}
        if len(rest) > 3:
            kwargs["src_name"] = rest[3]
        if len(rest) > 4:
            kwargs["key_suffix"] = rest[4]
        cache_pack(in_dir, key, out_dir, **kwargs)
        return 0
    if op == "release-url-parse":
        base = rest[0]
        parsed = gh_release_url_parse(base)
        if parsed is None:
            return 1
        owner_repo, tag = parsed
        print(f"{owner_repo}\t{tag}")
        return 0
    print(f"cache_io: unknown op {op!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
