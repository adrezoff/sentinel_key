"""Метрики Prometheus-совместимые (in-memory, для экспорта)."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(slots=True)
class Counter:
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount


@dataclass(slots=True)
class Gauge:
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        self.value = value


@dataclass(slots=True)
class Histogram:
    buckets: dict[float, int] = field(default_factory=lambda: {
        0.005: 0, 0.01: 0, 0.025: 0, 0.05: 0, 0.1: 0,
        0.25: 0, 0.5: 0, 1.0: 0, 2.5: 0, 5.0: 0, 10.0: 0,
    })
    sum: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        self.sum += value
        self.count += 1
        for bucket in sorted(self.buckets.keys()):
            if value <= bucket:
                self.buckets[bucket] += 1
                break


class MetricsCollector:
    """Централизованный сборщик метрик (thread-safe)."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = Lock()

    # --- Counters ---

    def counter(self, name: str, labels: dict[str, str] = None) -> Counter:
        key = self._make_key(name, labels or {})
        with self._lock:
            if key not in self._counters:
                self._counters[key] = Counter(labels=labels or {})
            return self._counters[key]

    def inc(self, name: str, amount: float = 1.0, labels: dict[str, str] = None) -> None:
        self.counter(name, labels).inc(amount)

    def get_counter(self, name: str, labels: dict[str, str] = None) -> Counter | None:
        key = self._make_key(name, labels or {})
        return self._counters.get(key)

    # --- Gauges ---

    def gauge(self, name: str, labels: dict[str, str] = None) -> Gauge:
        key = self._make_key(name, labels or {})
        with self._lock:
            if key not in self._gauges:
                self._gauges[key] = Gauge(labels=labels or {})
            return self._gauges[key]

    def set_gauge(self, name: str, value: float, labels: dict[str, str] = None) -> None:
        self.gauge(name, labels).set(value)

    # --- Histograms ---

    def histogram(self, name: str, labels: dict[str, str] = None) -> Histogram:
        key = self._make_key(name, labels or {})
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram()
            return self._histograms[key]

    def observe(self, name: str, value: float, labels: dict[str, str] = None) -> None:
        self.histogram(name, labels).observe(value)

    # --- Snapshot ---

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {k: {"value": v.value, "labels": v.labels} for k, v in self._counters.items()},
                "gauges": {k: {"value": v.value, "labels": v.labels} for k, v in self._gauges.items()},
                "histograms": {k: {"buckets": v.buckets, "sum": v.sum, "count": v.count} for k, v in self._histograms.items()},
            }

    @staticmethod
    def _make_key(name: str, labels: dict[str, str]) -> str:
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"


# Глобальный экземпляр
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    return _metrics


# --- Удобные функции для бизнес-метрик ---

class BusinessMetrics:
    """Высокоуровневые бизнес-метрики sentinel_key."""

    def __init__(self, collector: MetricsCollector = None):
        self._m = collector or get_metrics()

    def queue_size(self, kind: str, status: str, count: int) -> None:
        self._m.set_gauge("queue_size", count, {"kind": kind, "status": status})

    def oldest_queued_age_seconds(self, kind: str, age_seconds: float) -> None:
        self._m.set_gauge("oldest_queued_age_seconds", age_seconds, {"kind": kind})

    def commit_processed(self, count: int = 1) -> None:
        self._m.inc("commits_processed_total", count)

    def lines_processed(self, count: int) -> None:
        self._m.inc("lines_processed_total", count)

    def candidates_found(self, route: str, count: int = 1) -> None:
        self._m.inc("candidates_found_total", count, {"route": route})

    def llm_call(self, success: bool, latency_seconds: float, model: str = None) -> None:
        labels = {"model": model or "unknown"}
        self._m.inc("llm_calls_total", 1, {**labels, "success": str(success).lower()})
        self._m.observe("llm_latency_seconds", latency_seconds, labels)

    def incident_opened(self, severity: str) -> None:
        self._m.inc("incidents_opened_total", 1, {"severity": severity})

    def incident_reopened(self) -> None:
        self._m.inc("incidents_reopened_total", 1)

    def incident_closed(self, severity: str) -> None:
        self._m.inc("incidents_closed_total", 1, {"severity": severity})

    def incident_open_count(self, severity: str, count: int) -> None:
        self._m.set_gauge("incidents_open", count, {"severity": severity})

    def task_retry(self, kind: str, retryable: bool) -> None:
        self._m.inc("task_retries_total", 1, {"kind": kind, "retryable": str(retryable).lower()})

    def lease_expired(self, kind: str) -> None:
        self._m.inc("lease_expired_total", 1, {"kind": kind})

    def active_leases(self, kind: str, count: int) -> None:
        self._m.set_gauge("active_leases", count, {"kind": kind})
