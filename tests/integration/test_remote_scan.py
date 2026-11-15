"""Настоящий Git по локальному HTTP: кэш, история, повторный scan и ошибки."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import secrets
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from application import App
from git_collector import GitCollectorConfig, GitCollectorError, RemoteGitCollector
from git_collector.remote_cache import write_stamp


def git(path: Path, *args: str) -> bytes:
    """Выполнить Git для временной фикстуры, не используя внешнюю сеть."""
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, timeout=20,
    ).stdout.strip()


@pytest.fixture
def remote_server(tmp_path, monkeypatch):
    """Поднять временный Git HTTP backend с управляемым статусом ответа."""
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "config.py").write_text(
        f'production_password = "{secrets.token_urlsafe(36)}"\n', encoding="utf-8",
    )
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Initial")
    published = tmp_path / "repo.git"
    # Локальный clone может зависать на Windows при копировании object database.
    # Публикуем фикстуру через штатный push; HTTP clone проверяется ниже.
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(published))
    git(repository, "push", str(published), "main")
    state = {"requests": 0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        """Минимальный smart HTTP backend для настоящих clone/fetch в тестах."""

        def log_message(self, format, *args):
            """Не записывать URL и данные HTTP в тестовые логи."""

        def do_GET(self):
            """Передать Git HTTP-запрос локальному CGI backend."""
            self._serve()

        def do_POST(self):
            """Передать upload-pack локальному CGI backend."""
            self._serve()

        def _serve(self):
            state["requests"] += 1
            if state["status"] != 200:
                self.send_response(state["status"])
                self.send_header("Location", "/redirect-target")
                self.send_header("Retry-After", "900")
                self.end_headers()
                return
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            env = dict(os.environ, GIT_PROJECT_ROOT=str(tmp_path), GIT_HTTP_EXPORT_ALL="1",
                       PATH_INFO=parsed.path, QUERY_STRING=parsed.query,
                       REQUEST_METHOD=self.command, CONTENT_TYPE=self.headers.get("Content-Type", ""),
                       CONTENT_LENGTH=str(length), REMOTE_ADDR="127.0.0.1")
            response = subprocess.run(
                ["git", "http-backend"], input=body, env=env, capture_output=True, check=True, timeout=20,
            ).stdout
            headers, content = response.split(b"\r\n\r\n", 1)
            pairs = [line.decode("latin-1").split(":", 1) for line in headers.split(b"\r\n")]
            status = next((int(v.strip().split()[0]) for k, v in pairs if k.lower() == "status"), 200)
            self.send_response(status)
            for key, value in pairs:
                if key.lower() != "status":
                    self.send_header(key, value.strip())
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/repo.git"

    async def resolve_test_server(url):
        return f"127.0.0.1:{server.server_port}:127.0.0.1"

    # Единственное исключение SSRF — внутри теста, для временного localhost-сервера.
    monkeypatch.setattr("git_collector.remote.resolve_public_host", resolve_test_server)
    try:
        yield url, repository, published, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_remote_app_full_repeat_fetch_and_force_push(tmp_path, remote_server):
    """Сканирование URL сохраняет идемпотентность и обрабатывает изменённый граф."""
    url, repository, published, state = remote_server
    config = GitCollectorConfig(allow_http=True, remote_timeout_s=20)
    cache = tmp_path / "cache"
    fingerprint_key = secrets.token_bytes(32)
    async with App(tmp_path / "scan.db", fingerprint_key, git_cache=cache, git_config=config) as app:
        first = await app.scan(url)
        assert first.outcome.name == "OK"
        assert first.output.commits_done == 1
        assert first.output.candidates >= 1
        count = state["requests"]
        second = await app.scan(url + "/")
        assert second.outcome.name == "OK" and second.output.commits_new == 0
        assert state["requests"] == count
        stored = await app.storage.read(lambda session: session.get_repository_by_location(url))
        assert stored is not None and stored.location == url

    collector = RemoteGitCollector(url, config=config, mirror_root=cache)
    before = await collector.list_commits()
    diffs = [diff async for diff in collector.get_diff(before[0].sha)]
    assert diffs
    assert await collector.file_contains_in_head("config.py", lambda line: "production_password" in line)
    assert state["requests"] == count

    # Переписывание истории и новая ветка должны обнаруживаться по SHA, не по дате.
    git(repository, "commit", "--amend", "-qm", "Rewritten")
    git(repository, "checkout", "-qb", "feature")
    (repository / "extra.txt").write_text("feature\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "Feature")
    git(repository, "checkout", "main")
    git(repository, "merge", "--no-ff", "feature", "-m", "Merge")
    git(repository, "push", "--force", str(published), "--all")
    write_stamp(collector._updated, 1)
    write_stamp(collector._host_pause, 1)
    after = await collector.list_commits()
    assert before[0].sha not in {commit.sha for commit in after}
    assert len(after) == 3 and any(commit.is_merge for commit in after)
    async with App(tmp_path / "scan.db", fingerprint_key, git_cache=cache, git_config=config) as app:
        third = await app.scan(url)
        assert third.outcome.name == "OK" and third.output.commits_new == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 429, 503])
async def test_http_failures_do_not_retry_or_follow_redirects(tmp_path, remote_server, status):
    """Ошибки HTTP и редиректы не приводят к повторным сетевым запросам."""
    url, _, _, state = remote_server
    state["status"] = status
    collector = RemoteGitCollector(url, config=GitCollectorConfig(allow_http=True, remote_timeout_s=20), mirror_root=tmp_path / "cache")
    with pytest.raises(GitCollectorError):
        await collector.ensure_mirror()
    assert state["requests"] == 1
    with pytest.raises(GitCollectorError, match="cooldown"):
        await collector.ensure_mirror()
    assert state["requests"] == 1
