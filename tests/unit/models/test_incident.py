from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from models import (
    Candidate,
    DetectorFlags,
    Incident,
    IncidentEventKind,
    IncidentStatus,
    InvalidTransition,
    Route,
    SecretType,
    Severity,
)
NOW = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
FP = b"\x11" * 32


def _incident() -> Incident:
    return Incident(
        id=10,
        repository_id=1,
        fingerprint=FP,
        secret_type=SecretType.AWS_ACCESS_KEY,
        severity=Severity.HIGH,
        confidence=0.9,
        detector_flags=DetectorFlags.REGEX,
        evidence_masked="AKIA...MPLE",
        reason="trusted format",
        recommendation="rotate",
        first_commit_id=1,
    )


def _candidate() -> Candidate:
    return Candidate(
        id=5,
        repository_id=1,
        commit_id=2,
        file_path="env.py",
        line_number=3,
        secret_type=SecretType.AWS_ACCESS_KEY,
        value_masked="AKIA...MPLE",
        value_fingerprint=FP,
        entropy=4.1,
        detector_flags=DetectorFlags.REGEX,
        source_line_masked="key=AKIA...MPLE",
        context_masked="",
        is_test_context=False,
        score_regex=0.5,
        score_entropy=0.2,
        score_context=0.2,
        score_token_type=0.1,
        score_total=1.0,
        route=Route.SUSPICIOUS,
    )


class IncidentTransitionTests(unittest.TestCase):
    def test_open_to_acknowledged(self) -> None:
        incident = _incident()
        event = incident.set_status(IncidentStatus.ACKNOWLEDGED, "alice", "looking", now=NOW)
        self.assertEqual(incident.status, IncidentStatus.ACKNOWLEDGED)
        self.assertEqual(incident.version, 2)
        self.assertEqual(event.kind, IncidentEventKind.STATUS_CHANGED)
        self.assertIsNone(incident.resolved_at)

    def test_remediated_sets_resolved_at_and_reopen(self) -> None:
        incident = _incident()
        incident.set_status(IncidentStatus.REMEDIATED, "alice", None, now=NOW)
        self.assertEqual(incident.resolved_at, NOW)
        event = incident.set_status(IncidentStatus.OPEN, "system", "seen again", now=NOW)
        self.assertEqual(event.kind, IncidentEventKind.REOPENED)
        self.assertIsNone(incident.resolved_at)

    def test_illegal_ack_to_open(self) -> None:
        incident = _incident()
        incident.set_status(IncidentStatus.ACKNOWLEDGED, "a", None, now=NOW)
        with self.assertRaises(InvalidTransition):
            incident.set_status(IncidentStatus.OPEN, "a", None, now=NOW)

    def test_raise_severity_only_up(self) -> None:
        incident = _incident()
        incident.raise_severity(Severity.CRITICAL, now=NOW)
        self.assertEqual(incident.severity, Severity.CRITICAL)
        with self.assertRaises(ValueError):
            incident.raise_severity(Severity.HIGH, now=NOW)

    def test_add_occurrence(self) -> None:
        incident = _incident()
        occ = incident.add_occurrence(_candidate(), ai_task_id=None, now=NOW)
        self.assertEqual(occ.incident_id, 10)
        self.assertEqual(occ.candidate_id, 5)
        self.assertIsNone(occ.ai_task_id)


if __name__ == "__main__":
    unittest.main()
