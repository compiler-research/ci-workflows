#!/usr/bin/env python3
"""Ask the recipe cache whether it holds a cell, telling a miss from no answer.

Usage: probe.py BASE KEY
Exit:  0 the asset is there, 1 it is not, 2 the cache did not answer.

cache_io.cache_probe takes any error for a miss: a 5xx or a 429 from the
release-asset host, a timeout or a reset all read as "not there", and a
consumer that does not build on a miss fails with "no prebuilt" for an asset
that was published. Here only a 404 or a 410 is a miss; anything else is
retried, and reported as no answer when it persists.

This lives beside compute_key.py rather than in actions/lib/ because every
cache key hashes actions/lib/*.py: a change there moves every key and leaves
every published cell unreachable until it is published again.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import cache_io  # noqa: E402

ABSENT = (404, 410)
ATTEMPTS = 5


class NoAnswer(RuntimeError):
    """The cache did not say whether it holds the asset."""


def probe(base: str, key: str) -> bool:
    """Whether the asset for `key` is at `base`; NoAnswer if unknown."""
    if not base.startswith(("https://", "http://")):
        return cache_io.cache_probe(base, key)
    url = f"{base.rstrip('/')}/{key}.tar.zst"
    for attempt in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return 200 <= resp.status < 400
        except urllib.error.HTTPError as e:
            if e.code in ABSENT:
                return False
            err: Exception = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        if attempt + 1 < ATTEMPTS:
            time.sleep(2 ** attempt)
    raise NoAnswer(f"{url}: {err}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: probe.py BASE KEY", file=sys.stderr)
        return 64
    try:
        return 0 if probe(*argv) else 1
    except NoAnswer as e:
        print(f"probe: the cache did not answer: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
