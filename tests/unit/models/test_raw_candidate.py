from __future__ import annotations

import copy
import pickle
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from models import DetectorFlags, RawCandidate, Route, SecretType, ScoreBreakdown


FP = b"\xab" * 32
RAW = "AKIAIOSFODNN7EXAMPLE"


class RawCandidateTests(unittest.TestCase):
    def _raw(self) -> RawCandidate:
        return RawCandidate(
            repository_id=1,
            commit_id=2,
            file_path="app.py",
            line_number=10,
            raw_value=RAW,
            source_line_raw=f'key="{RAW}"',
            context_raw=["# secret", f'key="{RAW}"'],
            secret_type_hint=SecretType.AWS_ACCESS_KEY,
            value_fingerprint=FP,
            value_masked="AKIA...MPLE",
            key_name="AWS_ACCESS_KEY_ID",
        )

    def test_repr_hides_raw(self) -> None:
        text = repr(self._raw())
        self.assertNotIn(RAW, text)
        self.assertIn("AKIA...MPLE", text)

    def test_pickle_forbidden(self) -> None:
        raw = self._raw()
        with self.assertRaises(TypeError):
            pickle.dumps(raw)
        with self.assertRaises(TypeError):
            copy.copy(raw)
        with self.assertRaises(TypeError):
            copy.deepcopy(raw)

    def test_to_candidate_drops_raw_fields(self) -> None:
        candidate = self._raw().to_candidate(
            ScoreBreakdown(0.5, 0.2, 0.2, 0.1),
            DetectorFlags.REGEX | DetectorFlags.ENTROPY,
            Route.AI_VALIDATION,
            entropy=4.2,
            regex_rules=["aws_access_key"],
            source_line_masked='key="AKIA...MPLE"',
            context_masked="# secret",
            is_test_context=False,
        )
        self.assertFalse(hasattr(candidate, "raw_value"))
        self.assertFalse(hasattr(candidate, "source_line_raw"))
        self.assertEqual(candidate.value_masked, "AKIA...MPLE")
        self.assertAlmostEqual(candidate.score_total, 1.0)
        self.assertEqual(candidate.secret_type, SecretType.AWS_ACCESS_KEY)
        self.assertIsNone(candidate.id)


class ScoreBreakdownTests(unittest.TestCase):
    def test_total_capped(self) -> None:
        self.assertEqual(ScoreBreakdown(0.9, 0.9, 0.9, 0.9).total, 1.0)
        self.assertAlmostEqual(ScoreBreakdown(0.1, 0.1, 0.1, 0.1).total, 0.4)


if __name__ == "__main__":
    unittest.main()
