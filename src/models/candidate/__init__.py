"""Модели кандидатов статического анализа (сырой в памяти + сохраняемый)."""

from .candidate import Candidate, Route
from .raw_candidate import RawCandidate
from .score_breakdown import ScoreBreakdown

__all__ = [
    "Candidate",
    "RawCandidate",
    "Route",
    "ScoreBreakdown",
]
