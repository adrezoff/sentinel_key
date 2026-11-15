"""HTTP(S)-репозиторий: редкое обновление зеркала и локальный анализ истории."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
import hashlib
import os
from pathlib import Path
import signal
import tempfile
import time
from urllib.parse import urlsplit

from .config import GitCollectorConfig
from .errors import GitCollectorError
from .local import LocalGitCollector
from .remote_cache import cache_io, cache_lock, read_stamp, write_stamp
from .remote_security import (
    isolated_git_env, normalize_remote_url, resolve_public_host, safe_git_options,
)
from .types import CommitInfo, FileDiff


class RemoteGitCollector:
    """Хранит полное зеркало; сетевые операции разрешены только при планировании."""

    def __init__(
        self, location: str, *, config: GitCollectorConfig | None = None,
        mirror_root: str | Path | None = None,
    ) -> None:
        self.config = config or GitCollectorConfig()
        self.location = normalize_remote_url(location, allow_http=self.config.allow_http)
        self.mirror_root = Path(mirror_root or "var/git-mirrors").expanduser().resolve()
        key = hashlib.sha256(self.location.encode()).hexdigest()
        host = urlsplit(self.location).hostname
        host_key = hashlib.sha256(host.encode()).hexdigest()
        self.mirror_path = self.mirror_root / f"{key}.git"
        self._updated = self.mirror_root / f"{key}.updated.json"
        self._host_pause = self.mirror_root / f"{host_key}.pause.json"
        self._home = self.mirror_root / "empty-home"

    async def ensure_mirror(self) -> None:
        """Обновить зеркало с TTL, общим lock и устойчивой к рестарту паузой."""
        async with cache_lock(self.mirror_root):
            now = time.time()
            updated = await cache_io(read_stamp, self._updated)
            exists = await cache_io(self.mirror_path.is_dir)
            if exists and updated and now - updated < self.config.remote_cache_ttl_s:
                return
            pause = await cache_io(read_stamp, self._host_pause)
            if pause > now:
                raise GitCollectorError("remote host cooldown is active; retry later")
            # Сначала записываем паузу: авария процесса не разрешает немедленный повтор.
            await cache_io(
                write_stamp, self._host_pause,
                now + self.config.remote_timeout_s + self.config.remote_failure_cooldown_s,
            )
            try:
                await cache_io(self._home.mkdir, exist_ok=True, mode=0o700)
                async with asyncio.timeout(self.config.remote_timeout_s):
                    resolve = await resolve_public_host(self.location)
                    if exists:
                        await self._run_git([
                            "-C", str(self.mirror_path), "fetch", "--atomic", "--prune",
                            "--no-recurse-submodules", "--quiet", "--", self.location,
                            "+refs/*:refs/*",
                        ], resolve)
                    else:
                        await self._clone(resolve)
                finished = time.time()
                await cache_io(write_stamp, self._updated, finished)
                await cache_io(
                    write_stamp, self._host_pause,
                    finished + self.config.remote_host_interval_s,
                )
            except BaseException as exc:
                await asyncio.shield(cache_io(
                    write_stamp, self._host_pause,
                    time.time() + self.config.remote_failure_cooldown_s,
                ))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not isinstance(exc, Exception):
                    raise
                raise GitCollectorError("remote Git update failed; host cooldown applied") from None

    async def _clone(self, resolve: str) -> None:
        temporary = await cache_io(
            tempfile.TemporaryDirectory, prefix="clone-", dir=self.mirror_root,
        )
        try:
            target = Path(temporary.name) / "mirror.git"
            await self._run_git([
                "clone", "--mirror", "--template=", "--quiet", "--",
                self.location, str(target),
            ], resolve)
            await cache_io(target.replace, self.mirror_path)
        finally:
            await cache_io(temporary.cleanup)

    async def _run_git(self, args: Sequence[str], resolve: str) -> None:
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.config.git_executable, *safe_git_options(),
            "-c", f"http.curloptResolve={resolve}", *args,
            cwd=str(self.mirror_root), env=isolated_git_env(str(self._home)),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name != "nt",
        ))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            # Отмена создания процесса не должна оставить transport вне контроля.
            process = await spawning
            await asyncio.shield(_stop_process_tree(process))
            raise
        try:
            code = await process.wait()
        except BaseException:
            await asyncio.shield(_stop_process_tree(process))
            raise
        if code:
            raise GitCollectorError("remote Git command failed")

    def _local(self) -> LocalGitCollector:
        return LocalGitCollector(
            self.mirror_path, config=self.config,
            process_env=isolated_git_env(str(self._home)),
            process_options=[*safe_git_options(), "-c", "protocol.http.allow=never",
                             "-c", "protocol.https.allow=never"],
        )

    async def list_commits(self, refs: Sequence[str] | None = None) -> list[CommitInfo]:
        """Обновить зеркало при необходимости и прочитать локальный граф SHA."""
        if refs and any(ref.startswith("-") and ref != "--all" for ref in refs):
            raise ValueError("refs must not contain Git options except --all")
        await self.ensure_mirror()
        return await self._local().list_commits(refs)

    async def get_diff(self, sha: bytes) -> AsyncIterator[FileDiff]:
        """Прочитать diff из уже подготовленного зеркала без сетевых запросов."""
        async for diff in self._local().get_diff(sha):
            yield diff

    async def file_contains_in_head(
        self, path: str, line_matcher: Callable[[str], bool], *, ref: str | None = None,
    ) -> bool:
        """Проверить локальный HEAD зеркала, не обновляя refs во время анализа."""
        if ref and ref.startswith("-"):
            raise ValueError("ref must not be a Git option")
        return await self._local().file_contains_in_head(path, line_matcher, ref=ref)


async def _stop_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
