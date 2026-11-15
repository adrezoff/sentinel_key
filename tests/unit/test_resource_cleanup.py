"""Освобождение HTTP и SQLite при обычном и аварийном завершении."""

import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ai_validation.llm_client import RealLlmClient
from application import App


@pytest.mark.asyncio
async def test_real_client_closes_transport_once():
    """Созданный транспорт закрывается один раз без повторного создания."""
    transport = SimpleNamespace(close=AsyncMock())
    client = RealLlmClient()
    with patch("ai_validation.llm_client.openai.AsyncOpenAI", return_value=transport) as factory:
        await client.aclose()
        factory.assert_not_called()
        assert client._get_client() is transport
        await client.aclose()
        await client.aclose()
    transport.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("body_fails", [False, True])
async def test_app_closes_ai_on_context_exit(tmp_path, body_fails):
    """App освобождает клиент и БД даже при исключении внутри контекста."""
    app = App(tmp_path / "scan.db", secrets.token_bytes(32), ai_enabled=True, use_fake_llm=True)
    close = AsyncMock()
    app.ai_worker.llm_client = SimpleNamespace(aclose=close)
    with patch.object(app.storage, "close", wraps=app.storage.close) as storage_close:
        try:
            async with app:
                if body_fails:
                    raise RuntimeError("scan interrupted")
        except RuntimeError:
            assert body_fails
        close.assert_awaited_once()
        storage_close.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_closes_when_llm_cleanup_fails(tmp_path):
    """Ошибка закрытия HTTP не мешает освобождению SQLite."""
    app = App(tmp_path / "scan.db", secrets.token_bytes(32), ai_enabled=True, use_fake_llm=True)
    app.ai_worker.llm_client = SimpleNamespace(aclose=AsyncMock(side_effect=RuntimeError("close failed")))
    with patch.object(app.storage, "close", wraps=app.storage.close) as storage_close:
        with pytest.raises(RuntimeError, match="close failed"):
            async with app:
                pass
        storage_close.assert_awaited_once()
