from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))

from git_collector.errors import GitCollectorError
from git_collector.local.sha import parse_sha_hex, sha_to_hex


class ShaHelperTests(unittest.TestCase):
    def test_parse_sha1_and_roundtrip(self) -> None:
        hex_sha = "a" * 40
        raw = parse_sha_hex(hex_sha)
        self.assertEqual(len(raw), 20)
        self.assertEqual(sha_to_hex(raw), hex_sha)

    def test_parse_sha256(self) -> None:
        hex_sha = "b" * 64
        raw = parse_sha_hex(f"  {hex_sha}  ")
        self.assertEqual(len(raw), 32)
        self.assertEqual(sha_to_hex(raw), hex_sha)

    def test_reject_bad_length(self) -> None:
        with self.assertRaises(GitCollectorError):
            parse_sha_hex("abcd")

    def test_reject_non_hex(self) -> None:
        with self.assertRaises(GitCollectorError):
            parse_sha_hex("g" * 40)

    def test_sha_to_hex_rejects_wrong_size(self) -> None:
        with self.assertRaises(GitCollectorError):
            sha_to_hex(b"\x00" * 16)


if __name__ == "__main__":
    unittest.main()
