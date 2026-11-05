"""Observability: структурные логи (JSON) и метрики.

Никаких секретов в логах — только маскированные значения.
"""

from .logger import get_logger, configure_logging
from .metrics import MetricsCollector, get_metrics

__all__ = [
    "get_logger",
    "configure_logging", 
    "MetricsCollector",
    "get_metrics",
]
