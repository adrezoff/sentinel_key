from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import IncidentPolicy, ScoringConfig
from models import (
    Candidate,
    DetectorFlags,
    Route,
    SecretType,
    Severity,
)


FP = b"\xcd" * 32


def _candidate(**kwargs: object) -> Candidate:
    fields = dict(
        repository_id=1,
        commit_id=1,
        file_path="x",
        line_number=1,
        secret_type=SecretType.AWS_ACCESS_KEY,
        value_masked="AKIA...MPLE",
        value_fingerprint=FP,
        entropy=3.0,
        detector_flags=DetectorFlags.REGEX,
        source_line_masked="x",
        context_masked="",
        is_test_context=False,
        score_regex=0.5,
        score_entropy=0.2,
        score_context=0.2,
        score_token_type=0.1,
        score_total=1.0,
        route=Route.AI_VALIDATION,
    )
    fields.update(kwargs)
    return Candidate(**fields)  # type: ignore[arg-type]


@dataclass(frozen=True)
class _StubVerdict:
    is_secret: bool
    confidence: float


class ConfigTests(unittest.TestCase):
    def test_scoring_route(self) -> None:
        cfg = ScoringConfig()
        self.assertEqual(cfg.route_for(0.2), Route.IGNORE)
        self.assertEqual(cfg.route_for(0.3), Route.SUSPICIOUS)
        self.assertEqual(cfg.route_for(0.7), Route.AI_VALIDATION)

    def test_policy_ai_is_final(self) -> None:
        policy = IncidentPolicy()
        candidate = _candidate()
        verdict = _StubVerdict(is_secret=False, confidence=0.99)
        self.assertFalse(policy.should_open(candidate, verdict))

    def test_policy_local_trusted(self) -> None:
        policy = IncidentPolicy()
        self.assertTrue(policy.should_open(_candidate(), None))
        self.assertFalse(policy.should_open(_candidate(is_test_context=True), None))
        self.assertFalse(
            policy.should_open(_candidate(secret_type=SecretType.GENERIC_PASSWORD, score_total=1.0), None)
        )

    def test_bump_severity(self) -> None:
        policy = IncidentPolicy()
        self.assertEqual(policy.bump_severity(Severity.HIGH, still_in_head=True), Severity.CRITICAL)
        self.assertEqual(policy.bump_severity(Severity.CRITICAL, still_in_head=True), Severity.CRITICAL)
        self.assertEqual(policy.bump_severity(Severity.HIGH, still_in_head=False), Severity.HIGH)


if __name__ == "__main__":
    unittest.main()
