#!/usr/bin/env python3
"""Download and extract a cell from the recipe cache, retrying a broken fetch.

Usage: download.py BASE KEY OUT_DIR

cache_io.cache_download streams the asset through zstd into tar, so a
connection that ends early leaves a truncated archive behind and fails the
job, although the asset is fine. Here a fetch that breaks off is started
again into an empty OUT_DIR; only an answer that the asset is absent, a 404
or a 410, ends it at once.

Like probe.py, this lives beside compute_key.py rather than in actions/lib/,
which every cache key hashes.
"""

from __future__ import annotations

import shutil
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import cache_io  # noqa: E402

from probe import ABSENT, ATTEMPTS  # noqa: E402


def download(base: str, key: str, out_dir: str) -> None:
    out = Path(out_dir)
    for attempt in range(ATTEMPTS):
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        try:
            cache_io.cache_download(base, key, out_dir)
            return
        except urllib.error.HTTPError as e:
            if e.code in ABSENT or attempt + 1 == ATTEMPTS:
                raise
            err: Exception = e
        except Exception as e:  # a reset, a timeout, a truncated archive
            if attempt + 1 == ATTEMPTS:
                raise
            err = e
        print(f"download: {key}: {err}; retrying", file=sys.stderr)
        time.sleep(2 ** attempt)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: download.py BASE KEY OUT_DIR", file=sys.stderr)
        return 64
    download(*argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
