"""Unit tests for download: a fetch that breaks off is started again."""

from __future__ import annotations

import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import download


@mock.patch("download.time.sleep")
class DownloadTests(unittest.TestCase):
    def test_broken_fetch_is_retried_into_an_empty_dir(self, sleep):
        left_behind = []

        def fetch(base, key, out_dir):
            left_behind.append(sorted(p.name for p in Path(out_dir).iterdir()))
            if len(left_behind) == 1:
                (Path(out_dir) / "partial").write_bytes(b"x")
                raise tarfile.ReadError("unexpected end of data")

        with tempfile.TemporaryDirectory() as d:
            with mock.patch("download.cache_io.cache_download",
                            side_effect=fetch):
                download.download("https://cache/", "k", d + "/out")
        self.assertEqual(left_behind, [[], []])

    def test_absent_is_not_retried(self, sleep):
        err = urllib.error.HTTPError("https://cache/k", 404, "", {}, None)
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("download.cache_io.cache_download",
                            side_effect=err) as dl:
                with self.assertRaises(urllib.error.HTTPError):
                    download.download("https://cache/", "k", d + "/out")
            self.assertEqual(dl.call_count, 1)

    def test_gives_up_after_the_last_attempt(self, sleep):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("download.cache_io.cache_download",
                            side_effect=tarfile.ReadError("cut")) as dl:
                with self.assertRaises(tarfile.ReadError):
                    download.download("https://cache/", "k", d + "/out")
            self.assertEqual(dl.call_count, download.ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
