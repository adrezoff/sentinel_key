# ADR 0001: Зависимость `openai` для LLM-клиента

## Контекст
Для интеграции с NVIDIA Nemotron 3 Ultra через OpenAI-совместимый API требуется HTTP-клиент с поддержкой:
- Bearer token аутентификация
- Chat completions API
- Настройка `temperature`, `max_tokens`, `top_p`
- Отсутствие стриминга (один запрос — один ответ)

Стандартная библиотека `urllib` требует много boilerplate кода для работы с JSON, ошибками, ретраями. Библиотека `openai` — де-факто стандарт для OpenAI-совместимых API.

## Решение
Добавить зависимость `openai>=1.0.0` в проект. Использовать `openai.AsyncOpenAI` в `RealLlmClient`.

## Альтернативы
1. **Оставить `urllib`** — больше кода, больше мест для багов, ручная обработка ретраев/ошибок.
2. **`httpx` / `aiohttp`** — тоже сторонние зависимости, не дают готовую типизацию для OpenAI API.

## Последствия
- Новая зависимость в `requirements.txt` / `pyproject.toml` (после bootstrap)
- `RealLlmClient` упрощается: делегирует HTTP/JSON/ретраи библиотеке `openai`
- `FakeLlmClient` не меняется (для тестов)