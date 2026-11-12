"""Unit tests for probe: a miss is only an answer that says so."""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import probe


def _http_error(code):
    return urllib.error.HTTPError("https://cache/k.tar.zst", code, "", {}, None)


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@mock.patch("probe.time.sleep")
class ProbeHttpTests(unittest.TestCase):
    def test_not_found_is_a_miss(self, sleep):
        with mock.patch("probe.urllib.request.urlopen",
                        side_effect=_http_error(404)) as urlopen:
            self.assertEqual(probe.main(["https://cache/", "k"]), 1)
        self.assertEqual(urlopen.call_count, 1)

    def test_server_error_is_retried(self, sleep):
        with mock.patch("probe.urllib.request.urlopen",
                        side_effect=[_http_error(503),
                                     urllib.error.URLError("reset"),
                                     _Resp()]) as urlopen:
            self.assertEqual(probe.main(["https://cache/", "k"]), 0)
        self.assertEqual(urlopen.call_count, 3)

    def test_no_answer_is_not_a_miss(self, sleep):
        with mock.patch("probe.urllib.request.urlopen",
                        side_effect=_http_error(504)) as urlopen:
            self.assertEqual(probe.main(["https://cache/", "k"]), 2)
        self.assertEqual(urlopen.call_count, probe.ATTEMPTS)


class ProbeFileTests(unittest.TestCase):
    def test_file_scheme(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "k.tar.zst").write_bytes(b"x")
            self.assertEqual(probe.main([f"file://{d}", "k"]), 0)
            self.assertEqual(probe.main([f"file://{d}", "missing"]), 1)


if __name__ == "__main__":
    unittest.main()
