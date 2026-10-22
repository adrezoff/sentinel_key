"""Энтропия и выбор порога по алфавиту значения."""

from __future__ import annotations

import math
from collections import Counter

from config import ScoringConfig


def shannon_entropy(value: str) -> float:
    """Вычислить энтропию Шеннона в битах на символ."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def entropy_threshold(value: str, config: ScoringConfig) -> float:
    """Выбрать порог для hex, base64-подобного или произвольного алфавита."""
    if value and all(char in "0123456789abcdefABCDEF" for char in value):
        return config.entropy_hex_threshold
    if value and any(char.islower() for char in value) and all(char.isascii() and (char.isalnum() or char in "+/=_-") for char in value):
        return config.entropy_base64_threshold
    return config.entropy_other_threshold
