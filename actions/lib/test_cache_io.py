"""Unit tests for cache_io.

Stdlib unittest, runs on Linux / macOS / Windows alike. Each public
function gets one happy-path test plus targeted edge cases — minimal
coverage by design, the dry-run matrix is the broader integration
check.
"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cache_io


class ResolveCacheBaseTests(unittest.TestCase):
    def test_explicit_wins(self):
        with mock.patch.dict(os.environ, {"RECIPE_CACHE_BASE": "https://env/"}):
            self.assertEqual(
                cache_io.resolve_cache_base("https://explicit/"),
                "https://explicit/",
            )

    def test_env_when_no_explicit(self):
        with mock.patch.dict(os.environ, {"RECIPE_CACHE_BASE": "https://env/"}):
            self.assertEqual(cache_io.resolve_cache_base(None), "https://env/")
            self.assertEqual(cache_io.resolve_cache_base(""), "https://env/")

    def test_default_when_neither(self):
        env = {k: v for k, v in os.environ.items() if k != "RECIPE_CACHE_BASE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIn("compiler-research/ci-workflows",
                          cache_io.resolve_cache_base(None))


class GhReleaseUrlParseTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            cache_io.gh_release_url_parse(
                "https://github.com/owner/repo/releases/download/cache/"
            ),
            ("owner/repo", "cache"),
        )

    def test_no_trailing_slash(self):
        self.assertEqual(
            cache_io.gh_release_url_parse(
                "https://github.com/owner/repo/releases/download/v1"
            ),
            ("owner/repo", "v1"),
        )

    def test_non_github(self):
        self.assertIsNone(
            cache_io.gh_release_url_parse("https://example.com/releases/download/v1/")
        )

    def test_missing_marker(self):
        self.assertIsNone(
            cache_io.gh_release_url_parse("https://github.com/owner/repo/releases/")
        )

    def test_file_scheme(self):
        self.assertIsNone(cache_io.gh_release_url_parse("file:///tmp/cache/"))


class CacheProbeFileTests(unittest.TestCase):
    def test_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "k.tar.zst").write_bytes(b"x")
            self.assertTrue(cache_io.cache_probe(f"file://{d}", "k"))

    def test_absent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(cache_io.cache_probe(f"file://{d}", "missing"))

    def test_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            cache_io.cache_probe("ftp://nope", "k")


def _have_zstd() -> bool:
    try:
        subprocess.run(
            ["zstd", "--version"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@unittest.skipUnless(_have_zstd(), "zstd not available")
class CachePackRoundTripTests(unittest.TestCase):
    def _make_install_tree(self, root: Path) -> None:
        (root / "llvm-project" / "lib").mkdir(parents=True)
        (root / "llvm-project" / "lib" / "libfoo.a").write_bytes(b"binary")
        (root / "llvm-project" / "include").mkdir()
        (root / "llvm-project" / "include" / "foo.h").write_text("hdr\n")

    def test_pack_extract_diff(self):
        with tempfile.TemporaryDirectory() as d:
            in_dir = Path(d) / "in"
            in_dir.mkdir()
            self._make_install_tree(in_dir)

            out_dir = Path(d) / "out"
            out_dir.mkdir()
            cache_io.cache_pack(str(in_dir), "k", str(out_dir))

            asset = out_dir / "k.tar.zst"
            self.assertTrue(asset.is_file())
            self.assertGreater(asset.stat().st_size, 0)

            extract = Path(d) / "extract"
            extract.mkdir()
            cache_io.cache_download(f"file://{out_dir}", "k", str(extract))

            self.assertEqual(
                (extract / "llvm-project" / "include" / "foo.h").read_text(),
                "hdr\n",
            )
            self.assertEqual(
                (extract / "llvm-project" / "lib" / "libfoo.a").read_bytes(),
                b"binary",
            )

    def test_missing_input_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                cache_io.cache_pack(d, "k", d)


@unittest.skipUnless(_have_zstd(), "zstd not available")
class CacheUploadFileTests(unittest.TestCase):
    def test_file_backend_copies(self):
        with tempfile.TemporaryDirectory() as d:
            asset = Path(d) / "asset.tar.zst"
            asset.write_bytes(b"compressed")
            manifest = Path(d) / "manifest.json"
            manifest.write_text(json.dumps({"k": "v"}))
            dest = Path(d) / "cache"

            cache_io.cache_upload(
                f"file://{dest}", "k", str(asset), str(manifest)
            )

            self.assertEqual((dest / "k.tar.zst").read_bytes(), b"compressed")
            self.assertEqual(
                json.loads((dest / "k.manifest.json").read_text()),
                {"k": "v"},
            )

    def test_unsupported_backend_raises(self):
        with self.assertRaises(ValueError):
            cache_io.cache_upload(
                "ftp://nope/", "k", "/dev/null", "/dev/null"
            )


if __name__ == "__main__":
    unittest.main()
