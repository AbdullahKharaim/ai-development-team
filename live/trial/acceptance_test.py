"""Trusted acceptance tests for the first live task. Kept OUTSIDE the task worktree.

Codex's :workspace sandbox can read this file but cannot modify it.
Imported by run_tests.py with the worktree as the current directory.
"""

import unittest

from durations import format_seconds, parse_duration


class ParseDurationAcceptance(unittest.TestCase):
    def test_valid(self):
        cases = {"45s": 45, "2m": 120, "1h": 3600, "1h30m": 5400, "1h0m5s": 3605, "90m": 5400,
                 "2h15s": 7215, "0s": 0, "  3m  ": 180, "10h59m59s": 39599}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), want)

    def test_invalid(self):
        for text in ("", "   ", "abc", "5", "1x", "m5", "1m1h", "1h1h", "-5s", "1.5h", "1H",
                     "1h 30m", "h", "1h30"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)

    def test_non_string_rejected(self):
        for value in (None, 45, b"45s"):
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    parse_duration(value)

    def test_round_trip_with_format_seconds(self):
        for n in (0, 1, 59, 60, 61, 3599, 3600, 3661, 86399):
            with self.subTest(n=n):
                self.assertEqual(parse_duration(format_seconds(n)), n)


if __name__ == "__main__":
    unittest.main()
