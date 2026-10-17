"""Преобразование value object (payload, output, metrics) в JSON-совместимые структуры.

Storage сериализует эти dict; модели SQL не выполняют.
Новые dataclass payload/output подхватываются автоматически по типам полей.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from types import UnionType
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")


def to_jsonable(value: Any) -> Any:
    """Рекурсивно привести значение к типам, пригодным для JSON (enum → int, bytes → hex)."""
    if is_dataclass(value):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return int(value) if isinstance(value.value, int) else value.value
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return value.hex()
    return value


def from_jsonable(cls: type[T], data: Any) -> T:
    """Восстановить объект типа ``cls`` из JSON-совместимой структуры."""
    return _coerce(data, cls)  # type: ignore[return-value]


def _coerce(value: Any, annotation: Any) -> Any:
    if annotation is None or annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Union or origin is UnionType:
        non_none = [arg for arg in args if arg is not type(None)]
        if value is None:
            return None
        if not non_none:
            return value
        return _coerce(value, non_none[0])
    if origin in (list, tuple):
        inner = args[0] if args else Any
        return [_coerce(item, inner) for item in value]
    if origin is dict:
        key_t, val_t = args if len(args) == 2 else (str, Any)
        return {_coerce(k, key_t): _coerce(v, val_t) for k, v in value.items()}
    if origin is frozenset:
        inner = args[0] if args else Any
        return frozenset(_coerce(item, inner) for item in value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if is_dataclass(annotation) and isinstance(value, dict):
        hints = get_type_hints(annotation)
        kwargs = {}
        for field in fields(annotation):
            if field.name not in value:
                continue
            kwargs[field.name] = _coerce(value[field.name], hints.get(field.name, Any))
        return annotation(**kwargs)
    return value
