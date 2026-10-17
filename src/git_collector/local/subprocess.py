"""Run ``git`` without a shell; bound by timeout and optional semaphore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from ..config import GitCollectorConfig
from ..errors import GitCollectorError


class GitProcess:
    def __init__(self, cwd: str, config: GitCollectorConfig) -> None:
        self._cwd = cwd
        self._config = config
        self._semaphore = asyncio.Semaphore(config.git_workers)

    async def run(self, args: Sequence[str]) -> bytes:
        async with self._semaphore:
            return await self._communicate(args)

    async def stream_lines(self, args: Sequence[str]) -> AsyncIterator[bytes]:
        async with self._semaphore:
            proc = await asyncio.create_subprocess_exec(
                self._config.git_executable,
                *args,
                cwd=self._cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            assert proc.stderr is not None
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=self._config.command_timeout_s,
                        )
                    except asyncio.TimeoutError as exc:
                        await _kill(proc)
                        raise GitCollectorError(
                            f"git timed out after {self._config.command_timeout_s}s: "
                            f"git {' '.join(args)}"
                        ) from exc
                    if not line:
                        break
                    yield line
                stderr = await proc.stderr.read()
                code = await proc.wait()
            except Exception:
                await _kill(proc)
                raise
            if code != 0:
                raise GitCollectorError(
                    f"git exited with {code}: git {' '.join(args)}",
                    stderr=_decode(stderr),
                )

    async def _communicate(self, args: Sequence[str]) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            self._config.git_executable,
            *args,
            cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.command_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            await _kill(proc)
            raise GitCollectorError(
                f"git timed out after {self._config.command_timeout_s}s: git {' '.join(args)}"
            ) from exc
        if proc.returncode != 0:
            raise GitCollectorError(
                f"git exited with {proc.returncode}: git {' '.join(args)}",
                stderr=_decode(stderr),
            )
        return stdout


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.kill()
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()
