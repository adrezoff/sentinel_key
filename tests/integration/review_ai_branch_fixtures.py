"""Test fixtures for review AI branch checks."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from models import (
    AiValidationOutput, AiValidationPayload, AiVerdict, Candidate, Commit,
    DetectorFlags, ErrorCode, Outcome, Repository, Route, ScanMode,
    ScanTaskPayload, SecretType, Severity, Task, TaskKind, TaskResult,
    TaskStatus,
)
from models.common.clock import utc_now


def candidate(repository_id: int, commit_id: int) -> Candidate:
    """Create a test candidate with a GitHub token-like pattern."""
    return Candidate(
        repository_id=repository_id,
        commit_id=commit_id,
        file_path="config.py",
        line_number=1,
        key_name="api_key",
        secret_type=SecretType.GITHUB_TOKEN,
        value_masked="ghp_**...**",
        value_fingerprint=b"\x01" * 32,
        entropy=4.5,
        detector_flags=DetectorFlags.REGEX | DetectorFlags.ENTROPY,
        source_line_masked='api_key = "ghp_**...**"',
        context_masked='api_key = "ghp_**...**"\npassword = "***"',
        is_test_context=False,
        score_regex=0.8,
        score_entropy=0.7,
        score_context=0.3,
        score_token_type=0.9,
        score_total=0.85,
        route=Route.AI_VALIDATION,
        regex_rules=["GITHUB_TOKEN"],
        created_at=utc_now(),
    )


def verdict(c: Candidate, t: Task, **overrides) -> AiVerdict:
    """Create a test AI verdict for the given candidate and task."""
    from dataclasses import replace
    return replace(AiVerdict(
        task_id=t.id,
        candidate_id=c.id,
        is_secret=True,
        secret_type=SecretType.GITHUB_TOKEN,
        confidence=0.95,
        severity=Severity.HIGH,
        reason="Confirmed GitHub token",
        recommendation="Rotate the token",
        model="fake-model",
        prompt_version=1,
    ), **overrides)


def response(*, confidence=None, severity=None) -> dict:
    """Create a mock LLM response."""
    data = {
        "is_secret": True,
        "secret_type": "GITHUB_TOKEN",
        "confidence": 0.95 if confidence is None else confidence,
        "severity": "HIGH" if severity is None else severity,
        "reason": "Confirmed secret",
        "recommendation": "Rotate",
    }
    return data


class FakeLlmClient:
    """Fake LLM client that returns a predetermined response."""

    def __init__(self, response_data: dict):
        self._response = response_data
        self.prompt_version = 1

    async def complete(self, candidate, payload):
        from ai_validation.llm_client import parse_llm_response
        # Simulate real LLM client: parse and validate the response
        return parse_llm_response(
            json.dumps(self._response),
            candidate_id=payload.candidate_id,
            task_id=0,  # Will be set by AI worker
            model="fake-model",
            prompt_version=payload.prompt_version,
        )

    @property
    def max_concurrent(self):
        return 5


def client_with_response(response_data: dict):
    """Create a fake LLM client with the given response."""
    return FakeLlmClient(response_data)