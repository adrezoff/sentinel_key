"""Межпроцессная блокировка кэша и атомарные отметки сетевых операций."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from typing import BinaryIO, ParamSpec, TypeVar

from .errors import GitCollectorError

_T = TypeVar("_T")
_P = ParamSpec("_P")


async def cache_io(
    function: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs,
) -> _T:
    """Дождаться файловой операции даже при отмене, прежде чем отпустить lock."""
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


def read_stamp(path: Path) -> float:
    """Прочитать время; повреждённое состояние не должно сбрасывать лимит."""
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return 0.0
    except (ValueError, OSError):
        raise GitCollectorError("invalid remote cache state") from None
    if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise GitCollectorError("invalid remote cache timestamp")
    return float(value)


def write_stamp(path: Path, value: float) -> None:
    """Атомарно заменить отметку под общей блокировкой кэша."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="ascii") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _acquire(root: Path) -> BinaryIO:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = (root / "network.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise GitCollectorError("remote cache is busy; retry later") from None
    return stream


@asynccontextmanager
async def cache_lock(root: Path) -> AsyncIterator[None]:
    """Захватить общий лимит без ожидания; отмена не оставляет открытый lock."""
    pending = asyncio.create_task(asyncio.to_thread(_acquire, root))
    try:
        stream = await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            stream = await pending
        except GitCollectorError:
            pass
        else:
            stream.close()
        raise
    try:
        yield
    finally:
        stream.close()
