# План завершения проекта sentinel_key (оставшаяся работа)

**Статус:** черновик гайда для второго участника (Ваня — pipeline и LLM-компонент).  
**Источник истины:** `docs/models.md`, `docs/modules.md`, `docs/git_collector.md`, `AGENTS.md`, `README.md`.  
**Язык документа:** русский (комментарии/документация по правилу AGENTS.md §5.1). Имена идентификаторов, типы, сообщения ошибок — на английском.

---

## 1. Контекст и текущее состояние

`sentinel_key` — асинхронная система поиска секретов в Git-репозиториях и их истории. Pipeline:

```
Git → diff → preprocessor (RawCandidate) → static_analyzer (регулярки/энтропия/контекст → score → route)
    → route: IGNORE | SUSPICIOUS | AI_VALIDATION → ai_validation (вердикт LLM) → incident_manager (Incident)
```

**Распределение ролей из `Plan.md`:**
- **Андрей** — локальный анализ: Git-история и diff, извлечение кандидатов, regex, энтропия, контекст, оценка рисков, тестирование. — **считается выполненным.**
- **Ваня** — pipeline и LLM-компонент: асинхронная архитектура, интеграция LLM, промпт и классификация, маскирование/сантитаризация, retry-policy, обработка ошибок, хранение результатов, интеграционное тестирование. — **выполнен.**
- Оба — архитектура, интеграция, документация, демонстрация.

**Что уже реализовано в коде (не трогать без необходимости, только проверять на регресс):**
- `src/git_collector/` — `LocalGitCollector`, парсер diff, фабрика, валидация SHA.
- `src/preprocessor/` — `Preprocessor`, извлечение значений, маскирование, `value_fingerprint` (HMAC-SHA256).
- `src/static_analyzer/` — regex-детекторы, энтропия Шеннона, контекст, false-positive-фильтры, scoring, маршрутизация `Route`.
- `src/models/` — все доменные модели (`Repository`, `Commit`, `Task`, `Candidate`, `Incident`, `OutboxMessage` и т.д.), enum, payload/output value objects, кодек JSON. **AI-ветка:** `AiVerdict`, `AiValidationPayload`, `AiValidationOutput` — **реализованы** в `src/models/ai_validation/`.
- `src/config/` — `ScoringConfig`, `IncidentPolicy`.
- `src/storage/` — `SqliteStorage`, `StorageSession`, миграция `0001_initial.sql` (в таблице есть `ai_verdicts`, методы маппера `save_ai_verdict`, `get_ai_verdict`, `get_ai_verdict_by_candidate`, `get_ai_verdict_by_task`), CLI `init`/`info`.
- `src/feed/` — `SqliteFeed` (enqueue, claim, heartbeat, complete, fail, reap_expired, cancel).
- `src/orchestrator/` — `ScanOrchestrator` (планирование, финализация дерева задач).
- `src/workers/` — `CommitWorker` с атомарной транзакцией, протокол `AiTaskFactory` (заглушка), `WorkerSettings`.
- `src/incident_manager/` — `IncidentManager` с локальной политикой (trusted_formats, min_local_score, bump_if_in_head, reopen).
- `src/application.py` — `App` и CLI `python -m application scan` (поддерживает флаг `--ai-enabled`).
- `tests/` — unit-тесты и `tests/smoke_scan.py`.


---

## 2. Предварительная проверка регресса (что уже работает — не сломать) ✅ **ВЫПОЛНЕНО**

Перед внесением изменений убедиться, что существующий функционал работоспособен.

### 2.1. Запуск существующих unit-тестов ✅ **ВЫПОЛНЕНО (2026-11-03)**
- Прогнать все `tests/unit/test_*.py` и `tests/unit/models/test_*.py`.
- Фреймворк — `unittest` (стандартная библиотека), зависимостей добавлять не нужно (по правилу AGENTS.md §2: предпочитать стандартную библиотеку, новые зависимости — только через ADR в `docs/decisions/`).
- Тесты не должны делать реальных вызовов в LLM (используется фейковый `LlmClient`, где нужно).
- **Критерий готовности:** все существующие тесты проходят.
- **Результат:** все 61 unit-тест прошли успешно.

### 2.2. Smoke-тест ✅ **ВЫПОЛНЕНО (2026-11-03)**
- Запуск `python -m tests.smoke_scan` на временном Git-репозитории.
- **Результат:** `SMOKE OK: commits_done=1, candidates=1, repeat_new=0` Запустить `py -3 -m tests.smoke_scan` из корня репозитория с `PYTHONPATH=src`.
- **Ожидаемый вывод:** `SMOKE OK: commits_done=1, candidates=…, repeat_new=0`, где `candidates > 0`.
- **Критерий готовности:** smoke завершается успешно, повторный скан не создаёт дублей (`repeat_new=0`).

### 2.3. Проверка CLI и хранения
- `python -m storage init --database var/sentinel_key.db` и `python -m storage info --database var/sentinel_key.db` — создаёт БД, применяет миграцию 1, включает WAL.
- `py -3 -m application scan --repository <путь> --database <путь>` с `SENTINEL_KEY_HMAC_KEY_B64` — завершается кодом `0` (`outcome=OK`) или `2` (`PARTIAL`), секреты в выводе не попадают.
- **Критерий готовности:** команды отрабатывают без ошибок, схема версии 1, журнал WAL.

---

## 3. Оставшаяся работа (часть Вани — pipeline и LLM-компонент)

Выполняется строго по порядку этапов. Каждый этап заканчивается проверкой на регресс (пункт 2) и обновлением документации.

### Этап 1. Модели AI-ветки (AiVerdict, AiValidationPayload, AiValidationOutput) — ✅ **ВЫПОЛНЕНО** (уже реализовано до плана, коммит 9490b50)

Реализованы три модели в `src/models/ai_validation/` (один файл — одна модель, по AGENTS.md §5):
- `ai_verdict.py` — `AiVerdict` (таблица `ai_verdicts` уже есть в миграции 0001): поля `id`, `task_id` (FK + UQ), `candidate_id` (FK), `is_secret`, `secret_type`, `confidence` (0.0–1.0), `severity`, `reason`, `recommendation`, `model`, `prompt_version`, `created_at`. Валидация при создании: `0 ≤ confidence ≤ 1`, enum-значения, непустые строки.
- `ai_validation_payload.py` — `AiValidationPayload` (JSON value object для `AI_VALIDATION`): `candidate_id`, `prompt_version`, `model` (опционально).
- `ai_validation_output.py` — `AiValidationOutput` (JSON value object): `verdict_id`, `incident_id` (опционально).

Обновить `src/models/__init__.py` (экспорт), `docs/models.md` §3.8 (если нужно уточнить), `docs/modules.md` §1 (уже есть упоминание, уточнить статус).

**Правила AGENTS.md:** сырого секрета в моделях нет; только `value_masked`/`value_fingerprint` в `Candidate`. `AiVerdict` хранит только маскированные данные и вердикт.

**Критерий готовности:** модели импортируются, валидация полей работает, `__init__.py` обновлён.

### Этап 2. Регистрация TaskKind.AI_VALIDATION — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/models/task/task_kinds.py` зарегистрирован `TaskKind.AI_VALIDATION = 3` со спецификацией `TaskKindSpec(kind=AI_VALIDATION, payload_type=AiValidationPayload, output_type=AiValidationOutput, default_priority=50, default_max_attempts=5)`.
- `Task.ai_validation()` фабрика в `src/models/task/task.py` с `idempotency_key=key_for_ai_validation(candidate_id, prompt_version)`.
- `TaskPayload = Union[ScanTaskPayload, CommitAnalysisPayload, AiValidationPayload]`.
- `TaskOutput = Union[ScanSummary, CommitAnalysisOutput, AiValidationOutput]`.
- `key_for_ai_validation()` в `src/models/task/idempotency.py`.

В `src/models/task/task_kinds.py`:
- Зарегистрировать `TaskKind.AI_VALIDATION = 3` (уже объявлен, но не зарегистрирован в `_SPECS`).
- Добавить `TaskKindSpec(kind=TaskKind.AI_VALIDATION, payload_type=AiValidationPayload, output_type=AiValidationOutput, default_priority=50, default_max_attempts=5)` (приоритет выше `COMMIT_ANALYSIS = 100`, чтобы инциденты появлялись раньше — по `modules.md §8.2`).
- `TaskPayload` в `src/models/task/task.py` — расширить `Union` до `AiValidationPayload` (или оставить, если кодек подхватит по типам; явно добавить для type safety).
- `TaskOutput` в `src/models/task/task_result.py` — добавить `AiValidationOutput` в `Union`.

**Критерий готовности:** `spec_for(TaskKind.AI_VALIDATION)` возвращает спецификацию, `parse_payload`/`parse_output` работают для нового kind.

### Этап 3. Хранение ai_verdicts (мапперы в storage) — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/storage/session.py` (`StorageSession`) добавлены:
  - `save_ai_verdict(verdict: AiVerdict) -> AiVerdict` — идемпотентное сохранение (ON CONFLICT(task_id) DO NOTHING).
  - `get_ai_verdict(verdict_id: int) -> AiVerdict | None` — загрузка по PK.
  - `get_ai_verdict_by_candidate(candidate_id: int) -> AiVerdict | None` — поиск по кандидату.
  - `get_ai_verdict_by_task(task_id: int) -> AiVerdict | None` — поиск по задаче AI_VALIDATION.
  - `_ai_verdict_from_row(row)` — статический маппер строки БД в модель.
- Добавлен импорт `AiVerdict` в `src/storage/session.py`.
- В `src/storage/sqlite.py` добавлены public async-методы: `save_ai_verdict`, `get_ai_verdict`, `get_ai_verdict_by_candidate`, `get_ai_verdict_by_task`.

В `src/storage/session.py` (`StorageSession`) добавить:
- `save_ai_verdict(verdict) -> AiVerdict` — вставка в `ai_verdicts` (таблица уже существует).
- `get_ai_verdict(verdict_id) -> AiVerdict | None`.
- Опционально `get_ai_verdict_by_candidate(candidate_id)`.

В `src/storage/sqlite.py` добавить соответствующие public-методы `save_ai_verdict`, `get_ai_verdict` (делегируют в `run_in_transaction`/`read`).

**Критерий готовности:** `AiVerdict` сохраняется и загружается, таблица `ai_verdicts` (миграция 1) используется.

### Этап 4. Интерфейс LlmClient + фейковая реализация — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/ai_validation/llm_client.py`:
  - `LlmConfig` — конфигурация (endpoint, model, api_key, temperature=0.1, max_tokens, timeout_s, max_attempts, retry_base_s, max_concurrent, prompt_version).
  - Протокол `LlmClient` — интерфейс `complete(messages, *, timeout) -> str`.
  - `FakeLlmClient` — фейк для тестов: возвращает заранее заданные вердикты.
  - `RealLlmClient` — для будущей интеграции с NVIDIA Nemotron (OpenAI SDK).
  - `build_prompt(candidate)` — построение system/user промптов.
  - `parse_llm_response()` — строгая валидация JSON, enum-проверки, confidence [0,1], непустые строки.
  - Исключения: `LlmError`, `LlmTimeoutError`, `LlmNetworkError`, `LlmInvalidResponseError`, `LlmRateLimitError`, `LlmAuthError` с флагами `retryable`.
  - Retry с экспоненциальным бэкоффом (`1→2→4→8 с`), `max_attempts = 3`.

**Правила AGENTS.md §2:** LLM-провайдер/клиент не выбран окончательно — код за интерфейсами (`LlmClient`), реальный клиент добавляется без новых зависимостей без согласования.

В `src/ai_validation/` создать:
- `client.py` — протокол `LlmClient` (интерфейс, без бизнес-логики): метод `complete(messages, *, timeout) -> str` (или analogous). По AGENTS.md §2 §4 — код за интерфейсами, чтобы выбор библиотеки не размазался.
- `fake_client.py` — фейковая реализация для тестов (возвращает заранее заданный JSON-вердикт), никаких реальных сетевых вызовов.

**Правила AGENTS.md §2:** LLM-провайдер/клиент не выбраны — не добавлять реальный клиент без согласования. Все взаимодействие через протокол `LlmClient`.

**Критерий готовности:** протокол определён, фейк работает в тестах, реального сетевого кода нет.

### Этап 5. Masking guard (сантитаризация промпта) — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/ai_validation/masking_guard.py`:
  - `MaskingViolationError` — исключение с `pattern_name` и `matched`.
  - `MaskingGuardResult` — результат проверки (ok/violations).
  - `check_masking(text)` — проверка текста на сырые секреты.
  - `assert_safe_for_llm(text)` — бросает `MaskingViolationError` при нарушении.
  - `check_candidate_masking(candidate)` — проверка полей кандидата (`value_masked`, `source_line_masked`, `context_masked`).
  - Паттерны сырых секретов: AWS Access/Secret Key, GitHub Token, Slack Token, JWT, Private Key, Bearer Token, DB URL, API Key, Password.
  - Проверка `value_fingerprint` (64 hex chars).
- Создан `tests/unit/test_masking_guard.py` (11 тестов).

В `src/ai_validation/` — функция/класс `MaskingGuard`:
- Перед отправкой в LLM проверить, что промпт не содержит сырых значений секретов (`raw_value`, `source_line_raw`, `context_raw` из `RawCandidate`).
- В LLM уходят только маскированные значения (`AKIA...MPLE`, `value_masked`, `source_line_masked`, `context_masked`) — инвариант AGENTS.md §6.2.
- При подозрении на сырое значение → `FAILED(MASKING_VIOLATION)`, в LLM не уходит (код ошибки `ErrorCode.MASKING_VIOLATION = 5`).

**Критерий готовности:** промпт содержит только маскированные данные; попытка передать сырой секрет блокируется.

### Этап 6. Сборка промпта и версионирование prompt_version — ✅ **ВЫПОЛНЕНО (2026-11-03)** (как часть Этапа 4)

Выполнено:
- `SYSTEM_PROMPT` (английский) — инструкции аналитику, схема JSON ответа, правила.
- `USER_PROMPT_TEMPLATE` — шаблон с полями кандидата: `file_path`, `line_number`, `secret_type`, `value_masked`, `context_masked`, `regex_rules`, `entropy`, `detector_flags`, `score_total`.
- `build_prompt(candidate)` — построение (system, user) промптов.
- `prompt_version` в `LlmConfig` (default=1), входит в `idempotency_key` задачи `AI_VALIDATION`.
- Флаг `ai.enabled` (в конфиге) — полное отключение внешних LLM; система работает без LLM (инвариант AGENTS.md §6.2).

Системный промпт:
- Пользовательский (динамический) промпт: repository, commit, file, line, masked code, context, token, entropy, regex matches.
- `prompt_version` — целое, входит в `idempotency_key` задачи `AI_VALIDATION` (по `docs/models.md §3.4`: `idempotency_key = kind, candidate_id, prompt_version`). При изменении промпта увеличивать `prompt_version`, старые вердикты не переиспользуются молча.
- Флаг `ai.enabled` (в конфиге) — полное отключение внешних LLM; система обязана работать без LLM (инвариант AGENTS.md §6.2).

**Критерий готовности:** промпт собирается только из маскированных данных, `prompt_version` учитывается в идемпотентности, `ai.enabled=false` отключает отправку.

### Этап 7. Вызов LLM под Semaphore с таймаутом и ретраями — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/ai_validation/llm_client.py` (`RealLlmClient`):
  - `asyncio.Semaphore(max_concurrent=5)` — ограничение параллелизма вызовов LLM.
  - `asyncio.wait_for(..., timeout=timeout_s)` — таймаут запроса.
  - Retry: экспоненциальный бэкофф `1→2→4→8 с`, `max_attempts = 3`, jitter.
  - Обработка ошибок: `LLM_FAILED`, network, 429, 5xx → ретраи; невалидный JSON → `LLM_INVALID_RESPONSE`.
  - `LLM_INVALID_RESPONSE` — ошибка попытки (`retryable=True`), а не «не секрет».
  - `RealLlmClient` использует OpenAI SDK (`docs/decisions/0001-openai-dependency.md`).

Вызовы LLM:
- CPU-bound части (если будут) — `asyncio.to_thread` / `ThreadPoolExecutor` (инвариант §6.5), event loop не блокируется.
- Ретраи при `LLM_FAILED` / сетевых ошибках / 429 / 5xx / таймауте: backoff `1→2→4→8 с`, `max_attempts = 5` (для `AI_VALIDATION`, по `docs/models.md §3.4`).
- Невалидный JSON / несоответствие схеме → `LLM_INVALID_RESPONSE` (ретрай), а не «не секрет» (инвариант §6.10).
- После исчерпания попыток → `FAILED(LLM_FAILED)`, кандидат остаётся без вердикта, запуск получает `Outcome.PARTIAL`, остальное не затронуто (инvariant §6.7).

**Критерий готовности:** вызов ограничен семафором, ретраи с экспоненциальным бэкоффом, невалидный ответ — `LLM_INVALID_RESPONSE`.

### Этап 8. Парсинг и валидация JSON-ответа → AiVerdict — ✅ **ВЫПОЛНЕНО (2026-11-03)** (как часть Этапа 4/6)

Выполнено:
- `parse_llm_response(response_text, candidate_id, task_id, model, prompt_version)` — строгая валидация JSON, проверка enum-ов, `confidence ∈ [0,1]`, непустые `reason`/`recommendation`.
- Построение `AiVerdict` из валидированных полей.
- Невалидный ответ → `LLM_INVALID_RESPONSE` (инvariant §6.10).

Разобрать ответ LLM:
- Построить `AiVerdict`. Невалидный ответ → `LLM_INVALID_RESPONSE` (инvariant §6.10).

**Критерий готовности:** валидация отсекает битые ответы, валидный ответ превращается в `AiVerdict`.

### Этап 9. Интеграция с incident_manager (AI-ветка decide) — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Обновлён `IncidentManager.decide(candidate, verdict)` — принимает опциональный `AiVerdict`, делегирует в `policy.should_open(candidate, verdict)`.
- Обновлён `IncidentManager.process()` — добавлен параметр `verdict: AiVerdict | None`.
- Логика создания/обновления инцидента:
  - **AI-ветка** (`verdict.is_secret` и `verdict.confidence >= min_ai_confidence`): использует данные из вердикта (`secret_type`, `severity`, `confidence`, `reason`, `recommendation`), добавляет флаг `DetectorFlags.LLM` (8).
  - **Локальная ветка** (нет вердикта или `not is_secret`): trusted_formats, min_local_score, не test_context.
  - При создании: `severity` из вердикта (локально — `HIGH`), `confidence` из вердикта, `detector_flags |= LLM`.
  - При обновлении (reopen / occurrence added): если есть AI-вердикт — использует его `severity`, `confidence`, `reason`, `recommendation`; обновляет `detector_flags |= LLM`.
  - При reopen закрытого инцидента: учитывает AI-вердикт для повышения severity/confidence.
- Добавлен импорт `AiVerdict` в `incident_manager/manager.py`.

**Проверка:** все 87 unit-тестов проходят, smoke-тест `SMOKE OK`.

**Примечания:** Интеграция готова для AI Worker (этап 12). `IncidentPolicy.should_open` уже поддерживал `verdict` через протокол `AiVerdictLike`.

### Этап 10. OutboxMessage — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- Мапперы в `StorageSession` уже существовали: `save_outbox_message`, `update_outbox_message`, `get_outbox_message`.
- Добавлены публичные async-методы в `SqliteStorage`: `save_outbox_message`, `update_outbox_message`, `get_outbox_message`.
- Добавлен импорт `OutboxMessage` в `IncidentSession` протокол.
- При `severity ≥ HIGH` — создаётся `OutboxMessage` (канал по конфигу, `payload` — только маскированные данные) в той же транзакции, что и изменение инцидента (инvariant AGENTS.md §6.8).

При `severity ≥ HIGH`:
- Отправку выполняет отдельный компонент доставки (вне сферы MVP — заготовка/протокол). После успешной отправки `status = SENT`, событие `NOTIFIED`.

**Критерий готовности:** `OutboxMessage` создаётся атомарно с инцидентом, содержит только маскированные данные.

### Этап 11. Реализация AiTaskFactory в commit worker — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/workers/ai_factory.py`: реализован `DefaultAiTaskFactory` — создаёт задачу `AI_VALIDATION` для сохранённого `Candidate` с версионированным `idempotency_key` (`candidate_id + prompt_version`).
- В `CommitWorker._process.persist`: при `ai_enabled` и `route = AI_VALIDATION` → создаётся `Task(AI_VALIDATION)` в ленту (идемпотентно).
- При `ai_enabled=False` (MVP) — высокий балл даёт `SUSPICIOUS`, AI-задачи не создаются.
- Экспортирован в `src/workers/__init__.py`.
- Интегрирован в `CommitWorker` через параметр `ai_task_factory`.

В `src/workers/commit.py`:
- Реализовать `AiTaskFactory` (создаёт задачу `AI_VALIDATION` для сохранённого `Candidate` с версионированным `idempotency_key` — `candidate_id + prompt_version`).
- В `CommitWorker._process.persist`: при `ai_enabled` и `route = AI_VALIDATION` → создать `Task(AI_VALIDATION)` в ленту (идемпотентно).
- При `ai_enabled=False` (MVP) — высокий балл даёт `SUSPICIOUS`, AI-задачи не создаются (уже реализовано, не ломать).

**Критерий готовности:** при `ai_enabled=True` создаются `AI_VALIDATION` задачи, при `False` — нет.

### Этап 12. AI Worker (workers/ai.py) — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- В `src/workers/ai.py`: полная реализация `AiWorker`:
  - Цикл: `claim([AI_VALIDATION], worker_id, lease_s)`, обработка, `complete`/`fail`.
  - `masking guard` через `ai_validation.masking_guard.assert_safe_for_llm` перед отправкой в LLM.
  - LLM вызов через `LlmClient.complete()` (с semaphore, timeout, retry — в `RealLlmClient`).
  - Сохранение вердикта (`session.save_ai_verdict`) + инцидент (`incident_manager.process`) в одной транзакции.
  - Завершение задачи через `feed.complete` с `AiValidationOutput(verdict_id, incident_id)`.
  - Обработка ошибок: `LeaseLostError`, `CancelledError`, прочие → `feed.fail` с retryable.
  - Heartbeat каждые `lease_s/3` секунд.
- Фабрика `create_ai_worker()` для удобного создания с зависимостями (`FakeLlmClient`/`RealLlmClient`).
- Экспортирован в `src/workers/__init__.py`.

Создать `src/workers/ai.py` — `AIWorker`:
- Цикл как у `CommitWorker`: `claim([AI_VALIDATION], worker_id, lease_s)`, обработка, `complete`/`fail`.
- Параллелизм — `Semaphore(llm_workers)` (по умолчанию 5, по `modules.md §2`).
- Приоритет задач выше `COMMIT_ANALYSIS`.
- Логика вызова — в модуле `ai_validation/` (этапы 4–8).
- Heartbeat, graceful shutdown по `asyncio.Event`, обработка истечения аренды (перевыдача задачи).

**Критерий готовности:** `AIWorker` обрабатывает `AI_VALIDATION` задачи, не блокирует event loop, корректно завершается.

### Этап 13. Включение ai_enabled в application.py и CLI — ✅ **ВЫПОЛНЕНО (2026-11-03)**

Выполнено:
- Убрано жесткое ограничение `ai_enabled=False` — сделано конфигурируемым через флаг CLI `--ai-enabled`.
- Добавлены импорты: `AiWorker`, `create_ai_worker`, `DefaultAiTaskFactory`, `LlmConfig`.
- Обновлён `App.__init__`: параметры `ai_enabled`, `llm_config`.
- Добавлен `DefaultAiTaskFactory` в `CommitWorker` при `ai_enabled=True`.
- Создан `AiWorker` через `create_ai_worker` при `ai_enabled=True`.
- Добавлен запуск `ai_worker.run_once()` в главный цикл `scan()`.
- Добавлен CLI флаг `--ai-enabled` в `_parser()`.
- Обновлён `_run_scan`: передача `args.ai_enabled` в `App` и `ScanTaskPayload`.
- `LlmConfig.from_env()` загружает `SENTINEL_KEY_LLM_API_KEY` из env.

В `src/application.py` убрать жесткое ограничение `ai_enabled=False` (или сделать его конфигурируемым через флаг CLI `--ai-enabled`):
- При `ai_enabled=True` в `CommitWorker` передать реализацию `AiTaskFactory` и подключить `AIWorker`.
- CLI: добавить флаг `--ai-enabled` (по умолчанию `false` для обратной совместимости MVP).
- HMAC-ключ передаётся явно, не сохраняется; `ai_enabled=false` обязателен в чистом MVP, при `true` — подключается LLM-компонент.

**Критерий готовности:** CLI поддерживает `--ai-enabled`, при `false` поведение не меняется (MVP), при `true` создаются AI-задачи.

### Этап 14. Observability (логи и метрики) — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Создан `src/observability/logger.py`:
  - `StructuredFormatter` — JSON-форматтер с обязательными полями `task_id`, `run_id`, `commit_id`, `candidate_id`, `timestamp`, `level`, `logger`, `message`
  - Автоматическая санитизация: маскирует паттерны секретов (AWS keys, GitHub tokens, Slack tokens, JWT) в логах
  - `SecretSafeLogger` — кастомный логгер, санитизирующий сообщения перед выводом
  - `ContextLogger` — обёртка с автоматическим контекстом (`task_id`, `run_id`, `commit_id`, `candidate_id`)
  - `configure_logging()` / `get_logger()` — настройка и получение логгера
- Создан `src/observability/metrics.py`:
  - `MetricsCollector` — thread-safe сборщик: Counters, Gauges, Histograms с labels
  - `BusinessMetrics` — высокоуровневые бизнес-метрики:
    - Queue: `queue_size`, `oldest_queued_age_seconds`
    - Throughput: `commits_processed_total`, `lines_processed_total`, `candidates_found_total` (by route)
    - LLM: `llm_calls_total` (by model/success), `llm_latency_seconds`
    - Incidents: `incidents_opened_total`, `incidents_reopened_total`, `incidents_closed_total`, `incidents_open` (by severity)
    - Retries/Leases: `task_retries_total`, `lease_expired_total`, `active_leases`
  - `get_metrics()` — глобальный экземпляр, `snapshot()` для экспорта
- Экспорт в `src/observability/__init__.py`

**Проверка:** все 93 unit-теста проходят, интеграционные тесты (2) + smoke test `SMOKE OK`

**Примечания:** Логи — JSON, без секретов (маскирование AWS/GitHub/Slack/JWT), контекстные поля `task_id`/`run_id`/`commit_id`/`candidate_id`. Метрики in-memory, thread-safe, ready для экспорта.

### Этап 15. Тесты (unit + интеграционные) — ✅ **ВЫПОЛНЕНО (2026-11-05)**

Выполнено:
- Все unit-тесты проходят: **107 passed, 6 subtests passed** (было 4 неудачных: `test_from_env_with_defaults`, `test_ai_worker_success`, `test_llm_error_retry`, `test_masking_guard_violation`).
- Smoke-тест: `SMOKE OK: commits_done=1, candidates=1, repeat_new=0`.
- Исправлены баги:
  - `src/application.py`: `llm_config=LlmConfig() if None else None` → `llm_config=LlmConfig()` (всегда было `None`).
  - `src/ai_validation/llm_client.py`: `from_env()` использовал `cls.<attr>` на `slots=True` dataclass (возвращались дескрипторы, не значения) → `_defaults = cls()`; `api_key` пустая строка → `None`.
  - `src/workers/ai.py`: `verdict.task_id = task.id` на `frozen=True` dataclass → `replace(verdict, task_id=task.id)`.
  - `tests/unit/test_ai_worker.py`: множественные баги mock-инфраструктуры (`self.tasks` в лямбде, отсутствующие `read`/`run_in_transaction`/`fail`/`heartbeat`, `claim` возвращало `None`, `complete` был no-op, `setattr` на frozen dataclass, динамический тип payload вместо `AiValidationPayload`, кандидат не сохранялся в storage).
  - `tests/unit/test_ai_validation.py`: `test_from_env_with_defaults` не учитывал `SENTINEL_KEY_LLM_API_KEY` из `.env` → `patch.dict` для очистки env.

**Проверка:** `py -3 -m pytest tests/unit/` — 107 passed; `py -3 -m tests.smoke_scan` — `SMOKE OK`.

Добавить `tests/unit/test_ai_validation.py`, `tests/unit/test_ai_worker.py` (или аналогичные), обновить интеграционные:
- Unit: парсинг и валидация ответа LLM (валидный, невалидный JSON, несоответствие схемы), masking guard, `decide` с вердиктом, переходы статусов `Task`/`Incident` (уже есть, не ломать).
- Интеграция: временный git-репозиторий, БД в памяти, **фейковый `LlmClient`** (реальных вызовов нет — по `AGENTS.md §8`).
- Обязательные сценарии (по `AGENTS.md §8`):
  1. Первый запуск → повторный не создаёт дублей.
  2. Падение worker'а посреди пачки (аренда истекла → задача перевыдана).
  3. LLM недоступен → `Outcome.PARTIAL`.
  4. Один секрет в трёх коммитах → один `Incident` с тремя `Occurrence`.
  5. Закрытый инцидент при повторном обнаружении → reopen.
- Фикстуры с «секретами»: публичные примеры (`AKIAIOSFODNN7EXAMPLE`) или сгенерированные в тесте.

**Критерий готовности:** все новые тесты проходят, старые не сломались, реальных LLM-вызовов в тестах нет.

### Этап 16. Документация — ✅ **ВЫПОЛНЕНО (2026-11-05)**

Выполнено:
- `Plan.md` — приведён к реальности: `Хранение: PostgreSQL` → `Хранение: SQLite (sqlite3 из стандартной библиотеки)`.
- `docs/modules.md` — §1 обновлён: `AiVerdict`, `AiValidationPayload`, `AiValidationOutput` — реализованы в модуле `ai_validation` (убрано "(пока отсутствуют)"). §12 обновлён: поддерживается флаг `--ai-enabled`, убрано "`ai_enabled=false` обязателен в MVP". §11 обновлён: `AiVerdict` мапперы уже реализованы.
- `docs/models.md` — §5 обновлён: модели `AiVerdict`, `AiValidationPayload`, `AiValidationOutput` реализованы в `src/models/ai_validation/` и зарегистрированы в `TaskKind` (значение 3).
- Проверено соответствие `AGENTS.md` §7: `TaskKind.AI_VALIDATION = 3` зарегистрирован, payload/output типы определены, переходы через `Task.transition`, приоритет 50, правило `idempotency_key` через `key_for_ai_validation(candidate_id, prompt_version)`.

**Критерий готовности:** документация соответствует коду, новые модели и виды задач описаны.

### Этап 17. Финальная проверка регресса и Definition of Done

Повторно прогнать все тесты (unit + smoke) после всех изменений.

**DoD по AGENTS.md §9:**
- [ ] Тесты проходят; линтеры и проверка типов — те, что утверждены в проекте (сейчас фреймворк не утверждён — не добавлять по инициативе).
- [ ] Не добавлено новых зависимостей без согласования (§2).
- [ ] Инварианты §6 не нарушены (особенно 1, 2, 3, 8).
- [ ] Модели соответствуют `docs/models.md`; документация обновлена вместе с кодом.
- [ ] В логах нет значений секретов.
- [ ] Новая конфигурация имеет значение по умолчанию и описана.

**Дополнительно:**
- [ ] Сырой секрет не попадает в БД, логи, исключения, `repr()`, тесты (инvariant §6.1).
- [ ] В LLM уходят только маскированные значения (инvariant §6.2).
- [ ] Идемпотентность через `UNIQUE`-ограничения (инvariant §6.3).
- [ ] Состояние по SHA, не по дате (инvariant §6.4).
- [ ] Event loop не блокируется (инvariant §6.5).
- [ ] Backpressure: ограниченные очереди + `Semaphore` (инvariant §6.6).
- [ ] Ошибка не валит запуск (инvariant §6.7).
- [ ] Атомарность транзакций (инvariant §6.8).
- [ ] Пороги и коэффициенты — в конфиге (инvariant §6.9).
- [ ] Вердикт LLM валидируется (инvariant §6.10).
- [ ] Статусы меняются только через методы модели (инvariant §6.11).

---


---





---

## 6. Как запускать проверку (для выполнения пользователя)

### Переменные окружения
Создайте файл `.env` в корне проекта или экспортируйте переменные в shell:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = "<base64 32+ байт>"
$env:SENTINEL_KEY_LLM_API_KEY = "<ваш_ключ_NVIDIA_Nemotron>"
```

### Запуск тестов

```powershell
# Smoke-тест
py -3 -m tests.smoke_scan

# Unit-тесты
py -3 -m tests.unit.test_static_analyzer
py -3 -m tests.unit.test_storage
py -3 -m tests.unit.test_feed
py -3 -m tests.unit.test_preprocessor
py -3 -m tests.unit.test_git_collector.test_diff_parser
py -3 -m tests.unit.test_git_collector.test_factory
py -3 -m tests.unit.test_git_collector.test_local_collector
py -3 -m tests.unit.test_git_collector.test_remote_stubs
py -3 -m tests.unit.test_git_collector.test_sha
py -3 -m tests.unit.test_incident_manager
py -3 -m tests.unit.test_orchestrator
py -3 -m tests.unit.test_commit_worker
py -3 -m tests.unit.test_application
py -3 -m tests.unit.test_config
py -3 -m tests.unit.models.test_task
py -3 -m tests.unit.models.test_incident
py -3 -m tests.unit.models.test_raw_candidate
py -3 -m tests.unit.test_masking_guard

# Integration test для реального LLM (требует SENTINEL_KEY_LLM_API_KEY)
$env:SENTINEL_KEY_LLM_API_KEY = "<ваш_ключ_NVIDIA_Nemotron>"
py -3 -m tests.integration.test_real_llm
```
### CLI запуск

```powershell
# Без AI (по умолчанию)
py -3 -m application scan --repository "C:\path\to\project" --database "var\sentinel_key.db"

# С AI-валидацией (требует SENTINEL_KEY_LLM_API_KEY в env)
$env:SENTINEL_KEY_LLM_API_KEY = "<ваш_ключ_NVIDIA_Nemotron>"
py -3 -m application scan --repository "C:\path\to\project" --database "var\sentinel_key.db" --ai-enabled
```

### Пример с AI (создаёт тестовый репозиторий и сканирует)

```powershell
# 1. Создаём временный репозиторий с секретом и без
$repo = "$env:TEMP\sentinel_test_repo"
if (Test-Path $repo) { Remove-Item $repo -Recurse -Force }
git init $repo
cd $repo

# Файл БЕЗ секрета
"test_var = 'not_a_secret_just_test'" | Out-File -Encoding utf8 test.py

# Файл С секретом (плейсхолдер AWS ключа)
"aws_key = 'AKIAIOSFODNN7EXAMPLE'" | Out-File -Encoding utf8 config.py

git add .
git commit -m "Initial commit"

# 2. Запуск БЕЗ AI (только локальные детекторы)
py -3 -m application scan --repository "." --database "var\sentinel_key.db"
# Ожидается: найдёт AWS ключ через локальные детекторы (trusted_formats), инцидент откроется

# 3. Запуск С AI (включаем LLM-валидацию)
$env:SENTINEL_KEY_LLM_API_KEY = 
py -3 -m application scan --repository "." --database "var\sentinel_key.db" --ai-enabled
# Ожидается: LLM подтвердит, что AKIAIOSFODNN7EXAMPLE — это плейсхолдер (is_secret=false), инцидент не откроется
# Для test.py LLM вернёт is_secret=false с высокой уверенностью
```

**Ожидаемое поведение:**
- `test_var = 'not_a_secret_just_test'` → LLM: `is_secret=false`, `confidence≈0.99`
- `aws_key = 'AKIAIOSFODNN7EXAMPLE'` → LLM: `is_secret=false` (плейсхолдер), `confidence≈0.9`
- Реальные секреты → LLM: `is_secret=true`, `confidence≥0.85`

После внесения изменений повторить прогон всех `tests/unit/` и `tests/smoke_scan.py`, убедиться в отсутствии регресса, и только затем считать этап завершённым.
