"""Проверки ограничений URL, нагрузки и жизненного цикла зеркала без сети."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from git_collector import (
    GitCollectorConfig, GitCollectorError, RemoteGitCollector, SourceKind,
    create_git_collector, infer_source_kind, normalize_remote_url,
)
from git_collector.remote_cache import cache_lock, read_stamp, write_stamp
from git_collector.remote_security import isolated_git_env, resolve_public_host


@pytest.mark.parametrize("url", [
    "file:///tmp/repo", "ssh://git@github.com/org/repo", "git@github.com:org/repo",
    "https://user:password@github.com/org/repo", "https://github.com/r?token=hidden",
    "https://github.com/r#hidden", "https://github.com/a\nb", "https://github.com/../x",
    "https://github.com/%2e%2e/x", "https://github.com/a\\b", "https://github.com",
    "http://github.com/org/repo", "https://github.com:bad/org/repo",
    "https://[private-token]/repo",
])
def test_reject_unsafe_urls_without_echo(url):
    """Небезопасный URL отклоняется без его содержимого в исключении."""
    with pytest.raises(ValueError) as error:
        normalize_remote_url(url)
    assert url not in str(error.value)
    assert "password" not in str(error.value)
    with pytest.raises(ValueError) as factory_error:
        create_git_collector(url)
    assert url not in str(factory_error.value)
    assert "private-token" not in str(factory_error.value)


def test_normalization_and_generic_provider():
    """Нормализация сохраняет регистр пути и поддерживает произвольный хостинг."""
    url = "https://BITBUCKET.org:443/Team/Repo.git/"
    assert normalize_remote_url(url) == "https://bitbucket.org/Team/Repo.git"
    assert infer_source_kind(url) is SourceKind.REMOTE
    assert isinstance(create_git_collector(url), RemoteGitCollector)
    assert normalize_remote_url("http://host.example/repo", allow_http=True).startswith("http:")


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1", "fec0::1",
])
@pytest.mark.asyncio
async def test_private_and_mixed_dns_are_blocked(monkeypatch, address):
    """Даже один непубличный адрес в ответе DNS запрещает соединение."""
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
               for ip in ["8.8.8.8", address]]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records))
    with pytest.raises(GitCollectorError, match="public IP"):
        await resolve_public_host("https://host.example/repo")


@pytest.mark.asyncio
async def test_dns_is_pinned(monkeypatch):
    """Проверенный адрес передаётся Git без повторного разрешения DNS."""
    records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=records))
    assert await resolve_public_host("https://host.example/repo") == "host.example:443:8.8.8.8"


def test_environment_does_not_inherit_credentials_or_overrides(monkeypatch, tmp_path):
    """Окружение родителя не может включить trace, prompts или отключить TLS."""
    for key in ("GIT_CONFIG_COUNT", "GIT_TRACE", "GIT_SSL_NO_VERIFY", "HTTPS_PROXY",
                "GIT_ASKPASS", "SENTINEL_KEY_LLM_API_KEY", "GIT_DIR"):
        monkeypatch.setenv(key, "untrusted")
    env = isolated_git_env(str(tmp_path))
    assert "untrusted" not in env.values()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_HTTP_MAX_REQUESTS"] == "1"
    assert env["GIT_HTTP_MAX_RETRIES"] == "0"


@pytest.fixture
def transport(monkeypatch):
    """Подменить только сеть и создание зеркала, сохранив реальные lock и метки."""
    calls = []

    async def run(self, args, resolve):
        calls.append(list(args))
        if "clone" in args:
            Path(args[-1]).mkdir()

    monkeypatch.setattr(RemoteGitCollector, "_run_git", run)
    monkeypatch.setattr("git_collector.remote.resolve_public_host", AsyncMock(return_value="host.example:443:8.8.8.8"))
    return calls


@pytest.mark.asyncio
async def test_clone_cache_fetch_and_host_spacing(tmp_path, transport):
    """TTL действует между экземплярами, а интервал хоста — между репозиториями."""
    collector = RemoteGitCollector("https://host.example/team/repo", mirror_root=tmp_path)
    await collector.ensure_mirror()
    assert "clone" in transport[0]
    await RemoteGitCollector(collector.location, mirror_root=tmp_path).ensure_mirror()
    assert len(transport) == 1
    other = RemoteGitCollector("https://host.example/team/other", mirror_root=tmp_path)
    with pytest.raises(GitCollectorError, match="cooldown"):
        await other.ensure_mirror()
    write_stamp(collector._updated, 1)
    write_stamp(collector._host_pause, 1)
    await collector.ensure_mirror()
    assert "fetch" in transport[1] and "--atomic" in transport[1]
    assert len(transport) == 2


@pytest.mark.asyncio
async def test_failure_cooldown_survives_instances_and_hides_error(tmp_path, transport, monkeypatch):
    """Сбой удаляет незавершённый клон и запрещает повтор на всём хосте."""
    failing = AsyncMock(side_effect=GitCollectorError("private server output", stderr="secret"))
    monkeypatch.setattr(RemoteGitCollector, "_run_git", failing)
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path)
    with pytest.raises(GitCollectorError) as error:
        await collector.ensure_mirror()
    assert "private" not in str(error.value) and error.value.stderr is None
    assert not collector.mirror_path.exists()
    assert not list(tmp_path.glob("clone-*"))
    other = RemoteGitCollector("https://host.example/b", mirror_root=tmp_path)
    with pytest.raises(GitCollectorError, match="cooldown"):
        await other.ensure_mirror()
    assert failing.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_and_timeout_release_lock_and_apply_cooldown(tmp_path, transport, monkeypatch, cancel):
    """Отмена и таймаут оставляют паузу, но не удерживают блокировку кэша."""
    entered = asyncio.Event()

    async def stalled(*args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(RemoteGitCollector, "_run_git", stalled)
    config = replace(GitCollectorConfig(), remote_timeout_s=0.1)
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path, config=config)
    task = asyncio.create_task(collector.ensure_mirror())
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else GitCollectorError):
        await task
    async with cache_lock(tmp_path):
        assert read_stamp(collector._host_pause) > 0
    assert not list(tmp_path.glob("clone-*"))


@pytest.mark.asyncio
async def test_lock_blocks_another_process(tmp_path):
    """Второй процесс не может обновлять общий кэш одновременно с первым."""
    script = """
import asyncio, sys
from pathlib import Path
from git_collector.remote_cache import cache_lock
from git_collector import GitCollectorError
async def run():
    try:
        async with cache_lock(Path(sys.argv[1])):
            return 1
    except GitCollectorError:
        return 0
sys.exit(asyncio.run(run()))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"))
    async with cache_lock(tmp_path):
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", script, str(tmp_path)],
            env=env, capture_output=True, timeout=15,
        )
    assert result.returncode == 0, result.stderr
    async with cache_lock(tmp_path):
        pass


@pytest.mark.asyncio
async def test_corrupt_state_fails_closed(tmp_path, transport):
    """Повреждённая отметка не должна разрешать новые запросы."""
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path)
    collector._host_pause.write_text("invalid", encoding="ascii")
    with pytest.raises(GitCollectorError):
        await collector.ensure_mirror()
    assert not transport


@pytest.mark.parametrize("field,value", [
    ("remote_timeout_s", 0), ("remote_cache_ttl_s", float("nan")),
    ("remote_host_interval_s", -1), ("remote_failure_cooldown_s", 1),
])
def test_config_rejects_disabled_limits(field, value):
    """Лимиты нельзя случайно отключить нулём, NaN или отрицательным значением."""
    with pytest.raises(ValueError):
        GitCollectorConfig(**{field: value})


@pytest.mark.asyncio
async def test_git_options_in_refs_rejected_before_network(tmp_path, transport):
    """Пользовательский ref не должен превращаться в параметр записи файла."""
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path)
    with pytest.raises(ValueError):
        await collector.list_commits(["--output=leak.txt"])
    assert not transport


@pytest.mark.asyncio
async def test_git_process_is_reaped_on_cancellation(tmp_path, monkeypatch):
    """Отмена операции дожидается завершения дерева Git до освобождения lock."""
    entered = asyncio.Event()
    process = AsyncMock()

    async def wait():
        entered.set()
        await asyncio.Future()

    process.wait.side_effect = wait
    spawn = AsyncMock(return_value=process)
    stop = AsyncMock()
    monkeypatch.setattr("git_collector.remote.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("git_collector.remote._stop_process_tree", stop)
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path)
    task = asyncio.create_task(collector._run_git(["clone"], "host.example:443:8.8.8.8"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stop.assert_awaited_once_with(process)
    assert spawn.call_args.kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert spawn.call_args.kwargs["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_cancellation_during_spawn_still_reaps_process(tmp_path, monkeypatch):
    """Отмена во время запуска Git не оставляет неуправляемый процесс."""
    entered = asyncio.Event()
    release = asyncio.Event()
    process = AsyncMock()

    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    stop = AsyncMock()
    monkeypatch.setattr("git_collector.remote.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("git_collector.remote._stop_process_tree", stop)
    collector = RemoteGitCollector("https://host.example/a", mirror_root=tmp_path)
    task = asyncio.create_task(collector._run_git(["clone"], "host.example:443:8.8.8.8"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    stop.assert_awaited_once_with(process)
