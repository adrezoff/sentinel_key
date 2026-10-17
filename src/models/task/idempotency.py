"""``idempotency_key = sha256(...)[:16]`` (``docs/models.md`` §3.4)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .task_kinds import TaskKind

KEY_SIZE = 16


def short_key(*parts: object) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        payload = _encode(part)
        digest.update(len(payload).to_bytes(4, "big"))
        digest.update(payload)
    return digest.digest()[:KEY_SIZE]


def _encode(part: object) -> bytes:
    if isinstance(part, bytes):
        return part
    if isinstance(part, TaskKind):
        return int(part).to_bytes(4, "big", signed=False)
    if isinstance(part, bool):
        return b"\x01" if part else b"\x00"
    if isinstance(part, int):
        return part.to_bytes(8, "big", signed=True)
    if isinstance(part, str):
        return part.encode("utf-8")
    if isinstance(part, Sequence) and not isinstance(part, (bytes, str)):
        inner = hashlib.sha256()
        for item in part:
            chunk = _encode(item)
            inner.update(len(chunk).to_bytes(4, "big"))
            inner.update(chunk)
        return inner.digest()
    raise TypeError(f"unsupported idempotency part: {type(part)!r}")


def key_for_repository_scan(repository_id: int, nonce: bytes) -> bytes:
    return short_key(TaskKind.REPOSITORY_SCAN, repository_id, nonce)


def key_for_commit_analysis(repository_id: int, commit_ids: Sequence[int]) -> bytes:
    return short_key(TaskKind.COMMIT_ANALYSIS, repository_id, tuple(sorted(commit_ids)))
