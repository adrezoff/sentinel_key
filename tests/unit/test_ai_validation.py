# -*- coding: utf-8 -*-
"""Unit tests for ai_validation module: LLM response parsing, verdict validation, etc."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_validation.llm_client import (
    LlmConfig,
    LlmError,
    LlmTimeoutError,
    LlmNetworkError,
    LlmInvalidResponseError,
    LlmRateLimitError,
    LlmAuthError,
    parse_llm_response,
    build_prompt,
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)
from models.ai_validation.ai_verdict import AiVerdict
from models.ai_validation.ai_validation_payload import AiValidationPayload
from models.candidate import Candidate
from models.enums import SecretType, Severity, DetectorFlags


class LlmConfigTests(unittest.TestCase):
    def test_default_config(self) -> None:
        config = LlmConfig()
        self.assertEqual(config.model, "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(config.temperature, 0.1)
        self.assertEqual(config.max_tokens, 4000)
        self.assertEqual(config.timeout_s, 30.0)
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.retry_base_s, 1.0)
        self.assertEqual(config.max_concurrent, 5)
        self.assertEqual(config.prompt_version, 1)
        self.assertIsNone(config.api_key)

    def test_from_env_with_defaults(self) -> None:
        with patch.dict("os.environ", {"SENTINEL_KEY_LLM_API_KEY": ""}, clear=False):
            config = LlmConfig.from_env()
        self.assertEqual(config.api_endpoint, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(config.model, "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertIsNone(config.api_key)