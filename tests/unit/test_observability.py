# -*- coding: utf-8 -*-
"""Unit tests for observability: structured logging and metrics."""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from observability.logger import (
    ContextLogger,
    LogContext,
    SecretSafeLogger,
    StructuredFormatter,
    configure_logging,
    get_logger,
)
from observability.metrics import BusinessMetrics, MetricsCollector


class StructuredFormatterTests(unittest.TestCase):
    def test_formats_json_with_required_fields(self) -> None:
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="sentinel_key",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        record.task_id = 1
        record.run_id = 2
        record.commit_id = 3
        record.candidate_id = 4
        output = formatter.format(record)
        data = json.loads(output)
        self.assertEqual(data["level"], "INFO")
        self.assertEqual(data["message"], "test message")
        self.assertEqual(data["task_id"], 1)
        self.assertEqual(data["run_id"], 2)
        self.assertEqual(data["commit_id"], 3)
        self.assertEqual(data["candidate_id"], 4)
        self.assertEqual(data["service"], "sentinel_key")

    def test_masks_aws_key_in_extra_fields(self) -> None:
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="sentinel_key",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.aws_key = "AKIAIOSFODNN7EXAMPLE"
        output = formatter.format(record)
        data = json.loads(output)
        self.assertEqual(data["aws_key"], "***MASKED***")

    def test_masks_github_token_in_extra_fields(self) -> None:
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="sentinel_key",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.token = "ghp_123456789012345678901234567890123456"
        output = formatter.format(record)
        data = json.loads(output)
        self.assertEqual(data["token"], "***MASKED***")

    def test_passes_through_non_secret_values(self) -> None:
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="sentinel_key",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.commit_id = 42
        output = formatter.format(record)
        data = json.loads(output)
        self.assertEqual(data["commit_id"], 42)


class SecretSafeLoggerTests(unittest.TestCase):
    def test_sanitizes_aws_key_in_message(self) -> None:
        logger = SecretSafeLogger("test", logging.INFO)
        result = logger._sanitize_message("AKIAIOSFODNN7EXAMPLE")
        self.assertEqual(result, "AKIA***MASKED***")

    def test_sanitizes_github_token_in_message(self) -> None:
        logger = SecretSafeLogger("test", logging.INFO)
        result = logger._sanitize_message("ghp_123456789012345678901234567890123456")
        self.assertEqual(result, "ghp_***MASKED***")

    def test_passes_through_normal_message(self) -> None:
        logger = SecretSafeLogger("test", logging.INFO)
        result = logger._sanitize_message("normal log message")
        self.assertEqual(result, "normal log message")


class ContextLoggerTests(unittest.TestCase):
    def test_error_uses_error_level(self) -> None:
        """Метод error() должен записывать событие с уровнем ERROR."""
        logger = Mock()
        context_logger = ContextLogger(logger, LogContext(task_id=1))

        context_logger.error("failed")

        logger.log.assert_called_once()
        level, message = logger.log.call_args.args[:2]
        self.assertEqual(level, logging.ERROR)
        self.assertEqual(message, "failed")

    def test_with_context_creates_new_logger(self) -> None:
        logger = logging.Logger("test")
        ctx_logger = ContextLogger(logger, LogContext(task_id=1, run_id=2))
        new_logger = ctx_logger.with_context(candidate_id=3)
        self.assertIsNot(new_logger, ctx_logger)
        self.assertEqual(new_logger._context.task_id, 1)
        self.assertEqual(new_logger._context.run_id, 2)
        self.assertEqual(new_logger._context.candidate_id, 3)


class MetricsCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = MetricsCollector()

    def test_counter_increment(self) -> None:
        c = self.collector.counter("test_counter")
        c.inc()
        c.inc(5)
        self.assertEqual(c.value, 6.0)

    def test_counter_with_labels(self) -> None:
        c1 = self.collector.counter("test_counter", {"kind": "a"})
        c2 = self.collector.counter("test_counter", {"kind": "b"})
        c1.inc()
        c2.inc()
        self.assertEqual(c1.value, 1.0)
        self.assertEqual(c2.value, 1.0)
        self.assertEqual(len(self.collector._counters), 2)

    def test_gauge_set(self) -> None:
        g = self.collector.gauge("test_gauge")
        g.set(42.0)
        self.assertEqual(g.value, 42.0)

    def test_histogram_observe(self) -> None:
        h = self.collector.histogram("test_hist")
        h.observe(0.1)
        h.observe(0.5)
        self.assertEqual(h.count, 2)
        self.assertEqual(h.sum, 0.6)

    def test_snapshot(self) -> None:
        self.collector.inc("my_counter", labels={"kind": "a"})
        self.collector.set_gauge("my_gauge", 10.0)
        self.collector.observe("my_hist", 0.5)
        snap = self.collector.snapshot()
        self.assertIn("counters", snap)
        self.assertIn("gauges", snap)
        self.assertIn("histograms", snap)


class BusinessMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = MetricsCollector()
        self.metrics = BusinessMetrics(self.collector)

    def test_commit_processed(self) -> None:
        self.metrics.commit_processed()
        self.metrics.commit_processed(3)
        c = self.collector.get_counter("commits_processed_total")
        self.assertIsNotNone(c)
        self.assertEqual(c.value, 4.0)

    def test_candidates_found_by_route(self) -> None:
        self.metrics.candidates_found("AI_VALIDATION", 2)
        self.metrics.candidates_found("SUSPICIOUS", 1)
        c1 = self.collector.get_counter("candidates_found_total", {"route": "AI_VALIDATION"})
        c2 = self.collector.get_counter("candidates_found_total", {"route": "SUSPICIOUS"})
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        self.assertEqual(c1.value, 2.0)
        self.assertEqual(c2.value, 1.0)

    def test_llm_call_records_success_and_latency(self) -> None:
        self.metrics.llm_call(success=True, latency_seconds=0.5, model="nemotron")
        self.metrics.llm_call(success=False, latency_seconds=1.0)
        c_success = self.collector.get_counter("llm_calls_total", {"model": "nemotron", "success": "true"})
        c_fail = self.collector.get_counter("llm_calls_total", {"model": "unknown", "success": "false"})
        self.assertIsNotNone(c_success)
        self.assertIsNotNone(c_fail)
        self.assertEqual(c_success.value, 1.0)
        self.assertEqual(c_fail.value, 1.0)
        snap = self.collector.snapshot()
        self.assertIn("histograms", snap)

    def test_incident_opened_and_closed(self) -> None:
        self.metrics.incident_opened("HIGH")
        self.metrics.incident_closed("HIGH")
        c_open = self.collector.get_counter("incidents_opened_total", {"severity": "HIGH"})
        c_close = self.collector.get_counter("incidents_closed_total", {"severity": "HIGH"})
        self.assertIsNotNone(c_open)
        self.assertIsNotNone(c_close)
        self.assertEqual(c_open.value, 1.0)
        self.assertEqual(c_close.value, 1.0)


if __name__ == "__main__":
    unittest.main()