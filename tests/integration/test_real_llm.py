# -*- coding: utf-8 -*-
"""Integration test for real LLM client (minimal token usage)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from ai_validation.llm_client import RealLlmClient, LlmConfig
from models.ai_validation.ai_validation_payload import AiValidationPayload
from models.candidate import Candidate
from models.enums import SecretType, DetectorFlags


class RealLlmClientTest(unittest.IsolatedAsyncioTestCase):
    """Test real LLM client with NVIDIA Nemotron (minimal tokens)."""

    pytestmark = pytest.mark.real_llm

    @classmethod
    def setUpClass(cls) -> None:
        """Check API key is available and real-LLM tests were explicitly enabled."""
        if os.getenv("SENTINEL_KEY_RUN_REAL_LLM") != "1":
            raise unittest.SkipTest("set SENTINEL_KEY_RUN_REAL_LLM=1 to enable real LLM tests")
        cls.api_key = os.getenv("SENTINEL_KEY_LLM_API_KEY")
        if not cls.api_key:
            raise unittest.SkipTest("SENTINEL_KEY_LLM_API_KEY not set in environment")

    async def test_real_llm_minimal_call(self) -> None:
        """Test LLM with minimal prompt - should return valid JSON verdict."""
        config = LlmConfig(
            api_key=self.api_key,
            temperature=0.1,
            max_tokens=4000,
            timeout_s=30.0,
        )
        client = RealLlmClient(config)

        # Create a simple candidate with obvious non-secret
        candidate = Candidate(
            id=1,
            repository_id=1,
            commit_id=1,
            file_path="test.py",
            line_number=1,
            key_name="test_var",
            secret_type=SecretType.OTHER,
            value_masked="not_a_secret_just_test",
            value_fingerprint=b"x" * 32,
            entropy=2.0,
            regex_rules=[],
            detector_flags=DetectorFlags(0),
            source_line_masked="test_var = \"not_a_secret_just_test\"",
            context_masked="test_var = \"not_a_secret_just_test\"",
            is_test_context=True,
            score_regex=0.0,
            score_entropy=0.0,
            score_context=0.0,
            score_token_type=0.0,
            score_total=0.0,
            route=1,  # IGNORE
        )

        payload = AiValidationPayload(
            candidate_id=candidate.id,
            prompt_version=1,
            model=None,
        )

        verdict = await client.complete(candidate, payload)

        # Verify verdict structure
        self.assertIsInstance(verdict.is_secret, bool)
        self.assertIn(verdict.secret_type, list(__import__("models.enums", fromlist=["SecretType"]).SecretType))
        self.assertGreaterEqual(verdict.confidence, 0.0)
        self.assertLessEqual(verdict.confidence, 1.0)
        self.assertIn(verdict.severity, list(__import__("models.enums", fromlist=["Severity"]).Severity))
        self.assertIsInstance(verdict.reason, str)
        self.assertIsInstance(verdict.recommendation, str)
        self.assertEqual(verdict.model, "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(verdict.prompt_version, 1)

    async def test_real_llm_secret_detection(self) -> None:
        """Test LLM with a candidate that looks like a secret (but is masked)."""
        config = LlmConfig(
            api_key=self.api_key,
            temperature=0.1,
            max_tokens=4000,
            timeout_s=30.0,
        )
        client = RealLlmClient(config)

        candidate = Candidate(
            id=2,
            repository_id=1,
            commit_id=1,
            file_path="config.py",
            line_number=10,
            key_name="aws_key",
            secret_type=SecretType.AWS_ACCESS_KEY,
            value_masked="AKIA...MPLE",
            value_fingerprint=b"y" * 32,
            entropy=4.5,
            regex_rules=["aws_access_key"],
            detector_flags=DetectorFlags.REGEX,
            source_line_masked="aws_key = \"AKIA...MPLE\"",
            context_masked="aws_key = \"AKIA...MPLE\"\nother = 123",
            is_test_context=False,
            score_regex=0.9,
            score_entropy=0.5,
            score_context=0.3,
            score_token_type=0.8,
            score_total=0.8,
            route=3,  # AI_VALIDATION
        )

        payload = AiValidationPayload(
            candidate_id=candidate.id,
            prompt_version=1,
            model=None,
        )

        verdict = await client.complete(candidate, payload)

        # For a masked example, LLM should return is_secret=False with high confidence
        self.assertFalse(verdict.is_secret)
        self.assertLessEqual(verdict.confidence, 1.0)
        self.assertGreaterEqual(verdict.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()