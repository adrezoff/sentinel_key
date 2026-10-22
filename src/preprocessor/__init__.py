"""Предобработка Git diff в кандидаты, живущие только в памяти."""

from .config import PreprocessorConfig
from .processor import Preprocessor

__all__ = ["Preprocessor", "PreprocessorConfig"]
