from __future__ import annotations

import hashlib
import hmac
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from git_collector import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileChangeKind,
    FileDiff,
)
from models import SecretType
from preprocessor import Preprocessor, PreprocessorConfig

_HMAC_KEY = b"unit-test-fingerprint-key"


def _line(
    kind: DiffLineKind,
    text: str,
    *,
    old: int | None,
    new: int | None,
) -> DiffLine:
    return DiffLine(kind=kind, text=text, old_lineno=old, new_lineno=new)


def _file(*lines: DiffLine, path: str = "config/app.env") -> FileDiff:
    return FileDiff(
        path=path,
        change_kind=FileChangeKind.MODIFIED,
        hunks=(
            DiffHunk(
                old_start=1,
                old_count=1,
                new_start=1,
                new_count=len(lines),
                lines=tuple(lines),
            ),
        ),
    )


class PreprocessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.preprocessor = Preprocessor(_HMAC_KEY)

    def _process(self, file_diff: FileDiff) -> list:
        return list(
            self.preprocessor.process(file_diff, repository_id=7, commit_id=11)
        )

    def test_extracts_assignment_variants_with_model_metadata(self) -> None:
        diff = _file(
            _line(DiffLineKind.ADD, 'API_KEY="alpha-secret-value"', old=None, new=10),
            _line(DiffLineKind.ADD, "password: beta-secret-value", old=None, new=11),
            _line(DiffLineKind.ADD, "export TOKEN=gamma-secret-value", old=None, new=12),
        )

        candidates = self._process(diff)

        self.assertEqual(
            [(item.key_name, item.raw_value) for item in candidates],
            [
                ("API_KEY", "alpha-secret-value"),
                ("password", "beta-secret-value"),
                ("TOKEN", "gamma-secret-value"),
            ],
        )
        first = candidates[0]
        self.assertEqual(first.repository_id, 7)
        self.assertEqual(first.commit_id, 11)
        self.assertEqual(first.file_path, "config/app.env")
        self.assertEqual(first.line_number, 10)
        self.assertEqual(first.secret_type_hint, SecretType.UNKNOWN)
        self.assertEqual(
            first.value_fingerprint,
            hmac.new(_HMAC_KEY, b"alpha-secret-value", hashlib.sha256).digest(),
        )

    def test_extracts_bearer_url_password_and_bare_token(self) -> None:
        bare = "AbCdEf0123456789_XYZ-token"
        diff = _file(
            _line(
                DiffLineKind.ADD,
                "Authorization: Bearer header.payload.signature",
                old=None,
                new=20,
            ),
            _line(
                DiffLineKind.ADD,
                "DATABASE_URL=postgres://service:s3cr3t@db.internal/app",
                old=None,
                new=21,
            ),
            _line(DiffLineKind.ADD, f"// {bare}", old=None, new=22),
        )

        candidates = self._process(diff)

        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0].raw_value, "header.payload.signature")
        self.assertEqual(candidates[0].key_name, "Authorization")
        self.assertEqual(candidates[0].secret_type_hint, SecretType.BEARER_TOKEN)
        self.assertEqual(candidates[1].raw_value, "s3cr3t")
        self.assertEqual(candidates[1].secret_type_hint, SecretType.DATABASE_URL)
        self.assertEqual(candidates[2].raw_value, bare)

    def test_masks_value_source_and_repeated_value_in_context(self) -> None:
        raw = "AKIAIOSFODNN7EXAMPLE"
        neighboring = "neighboring-secret-value"
        diff = _file(
            _line(
                DiffLineKind.CONTEXT,
                f"# {neighboring} {raw}",
                old=1,
                new=1,
            ),
            _line(DiffLineKind.ADD, f'AWS_KEY="{raw}"', old=None, new=2),
            _line(DiffLineKind.CONTEXT, "next_setting=true", old=2, new=3),
        )

        candidate = self._process(diff)[0]

        self.assertEqual(candidate.value_masked, "AKIA...MPLE")
        self.assertNotIn(raw, candidate.source_line_masked)
        self.assertNotIn(raw, candidate.context_masked)
        self.assertNotIn(neighboring, candidate.context_masked)
        self.assertIn("AKIA...MPLE", candidate.source_line_masked)
        self.assertIn("AKIA...MPLE", candidate.context_masked)
        self.assertEqual(
            candidate.context_raw,
            [f"# {neighboring} {raw}", "next_setting=true"],
        )

    def test_masks_all_tokens_when_source_contains_more_than_one(self) -> None:
        first = "first_token_0123456789"
        second = "second_token_9876543210"
        diff = _file(
            _line(
                DiffLineKind.ADD,
                f"// {first} {second}",
                old=None,
                new=8,
            )
        )

        candidates = self._process(diff)

        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertNotIn(first, candidate.source_line_masked)
            self.assertNotIn(second, candidate.source_line_masked)

    def test_uses_only_added_lines_and_excludes_deleted_context(self) -> None:
        diff = _file(
            _line(DiffLineKind.DELETE, "OLD_SECRET=deleted-secret-value", old=4, new=None),
            _line(DiffLineKind.ADD, "NEW_SECRET=added-secret-value", old=None, new=4),
        )

        candidates = self._process(diff)

        self.assertEqual([item.raw_value for item in candidates], ["added-secret-value"])
        self.assertEqual(candidates[0].context_raw, [])

    def test_deduplicates_same_location_and_fingerprint_across_hunks(self) -> None:
        repeated = _line(
            DiffLineKind.ADD,
            "TOKEN=duplicate-secret-value",
            old=None,
            new=5,
        )
        diff = FileDiff(
            path="app.env",
            change_kind=FileChangeKind.MODIFIED,
            hunks=(
                DiffHunk(1, 1, 5, 1, (repeated,)),
                DiffHunk(20, 1, 5, 1, (repeated,)),
            ),
        )

        self.assertEqual(len(self._process(diff)), 1)

    def test_extracts_added_multiline_private_key(self) -> None:
        diff = _file(
            _line(
                DiffLineKind.ADD,
                "-----BEGIN RSA PRIVATE KEY-----",
                old=None,
                new=30,
            ),
            _line(DiffLineKind.ADD, "cHVibGljLWV4YW1wbGUtZGF0YQ==", old=None, new=31),
            _line(
                DiffLineKind.ADD,
                "-----END RSA PRIVATE KEY-----",
                old=None,
                new=32,
            ),
        )

        candidates = self._process(diff)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].secret_type_hint, SecretType.PRIVATE_KEY)
        self.assertEqual(candidates[0].line_number, 30)
        self.assertIn("\n", candidates[0].raw_value)
        self.assertNotIn("cHVibGlj", candidates[0].context_masked)

    def test_skips_binary_ignored_and_overlong_inputs(self) -> None:
        binary = FileDiff(
            path="image.bin",
            change_kind=FileChangeKind.MODIFIED,
            is_binary=True,
            skip_reason="binary file",
        )
        vendor = _file(
            _line(DiffLineKind.ADD, "TOKEN=vendor-secret-value", old=None, new=1),
            path="vendor/lib.env",
        )
        lock = _file(
            _line(DiffLineKind.ADD, "TOKEN=lock-secret-value", old=None, new=1),
            path="package-lock.json",
        )
        configured = Preprocessor(
            _HMAC_KEY,
            PreprocessorConfig(max_line_len=8),
        )
        overlong = _file(
            _line(DiffLineKind.ADD, "TOKEN=too-long", old=None, new=1)
        )

        self.assertEqual(self._process(binary), [])
        self.assertEqual(self._process(vendor), [])
        self.assertEqual(self._process(lock), [])
        self.assertEqual(
            list(configured.process(overlong, repository_id=1, commit_id=1)),
            [],
        )

    def test_short_values_are_fully_hidden(self) -> None:
        self.assertEqual(self.preprocessor.mask("s3cr3t"), "******")

    def test_rejects_empty_or_non_bytes_fingerprint_key(self) -> None:
        with self.assertRaises(ValueError):
            Preprocessor(b"")
        with self.assertRaises(ValueError):
            Preprocessor("not-bytes")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
