#!/usr/bin/env python3
"""Unit tests for crash_sweep."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crash_sweep as cs  # noqa: E402

CRASHED = "test session starts\ncollected 1 item\n"
SKIPPED = ("test session starts\n"
           "=========== 1 skipped in 0.42s ============\n")
XFAILED = ("test session starts\n"
           "=========== 1 xfailed in 1.20s ============\n")
XPASSED = ("test session starts\n"
           "FAILED t.py::T::test01 - [XPASS(strict)] crashes here\n"
           "=========== 1 failed in 1.20s =============\n")


class TestClassify(unittest.TestCase):
    def test_a_killed_interpreter_prints_no_summary(self):
        # The exit code cannot be used: LLVM's signal handler catches the
        # fault and the process still leaves with a plain status.
        self.assertEqual(cs.classify(CRASHED, timed_out=False),
                         "still crashes (no report)")

    def test_a_skip_is_not_a_crash(self):
        # A skipped test also exits 0 without the words a pass would print;
        # reading that as a crash is what the shell version did.
        self.assertEqual(cs.classify(SKIPPED, timed_out=False),
                         "not run here (skipped)")

    def test_a_marker_that_stopped_crashing_is_flagged(self):
        self.assertEqual(cs.classify(XPASSED, timed_out=False), cs.STALE)

    def test_a_marker_still_earning_its_keep_just_fails(self):
        self.assertEqual(cs.classify(XFAILED, timed_out=False), "still fails")

    def test_a_timeout_outranks_a_missing_summary(self):
        self.assertEqual(cs.classify(CRASHED, timed_out=True), "hangs")

    def test_an_xpass_outranks_a_timeout(self):
        # Both can hold when a test passes and then hangs on teardown; the
        # stale marker is the actionable half.
        self.assertEqual(cs.classify(XPASSED, timed_out=True), cs.STALE)

    def test_a_summary_word_in_prose_is_not_a_summary_line(self):
        self.assertEqual(
            cs.classify("a test that passed before now crashes\n",
                        timed_out=False),
            "still crashes (no report)")


if __name__ == "__main__":
    unittest.main()
