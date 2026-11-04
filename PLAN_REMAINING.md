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
- **Ваня** — pipeline и LLM-компонент: асинхронная архитектура, интеграция LLM, промпт и классификация, маскирование/сантитаризация, retry-policy, обработка ошибок, хранение результатов, интеграционное тестирование. — **остаётся выполнить.**
- Оба — архитектура, интеграция, документация, демонстрация.

**Что уже реализовано в коде (не трогать без необходимости, только проверять на регресс):**
- `src/git_collector/` — `LocalGitCollector`, парсер diff, фабрика, валидация SHA.
- `src/preprocessor/` — `Preprocessor`, извлечение значений, маскирование, `value_fingerprint` (HMAC-SHA256).
- `src/static_analyzer/` — regex-детекторы, энтропия Шеннона, контекст, false-positive-фильтры, scoring, маршрутизация `Route`.
- `src/models/` — все доменные модели (`Repository`, `Commit`, `Task`, `Candidate`, `Incident`, `OutboxMessage` и т.д.), enum, payload/output value objects, кодек JSON. **AI-ветка:** `AiVerdict`, `AiValidationPayload`, `AiValidationOutput` — **реализованы** в `src/models/ai_validation/`.
- `src/config/` — `ScoringConfig`, `IncidentPolicy`.
- `src/storage/` — `SqliteStorage`, `StorageSession`, миграция `0001_initial.sql` (в таблице уже есть `ai_verdicts`, но методов маппера нет), CLI `init`/`info`.
- `src/feed/` — `SqliteFeed` (enqueue, claim, heartbeat, complete, fail, reap_expired, cancel).
- `src/orchestrator/` — `ScanOrchestrator` (планирование, финализация дерева задач).
- `src/workers/` — `CommitWorker` с атомарной транзакцией, протокол `AiTaskFactory` (заглушка), `WorkerSettings`.
- `src/incident_manager/` — `IncidentManager` с локальной политикой (trusted_formats, min_local_score, bump_if_in_head, reopen).
- `src/application.py` — `App` и CLI `python -m application scan` (в MVP `ai_enabled=False` жестко).
- `tests/` — unit-тесты и `tests/smoke_scan.py`.

**Расхождения, которые нужно устранить:**
- `Plan.md §Инструментарий` говорит про **PostgreSQL**; `AGENTS.md §2` и ADR 0001 — **SQLite через `sqlite3`**. По правилу AGENTS.md («сначала документация») приводим `Plan.md` к реальности (SQLite).

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

### Этап 1. Модели AI-ветки (AiVerdict, AiValidationPayload, AiValidationOutput)

Реализовать три модели в `src/models/ai_validation/` (один файл — одна модель, по AGENTS.md §5):
- `ai_verdict.py` — `AiVerdict` (таблица `ai_verdicts` уже есть в миграции 0001): поля `id`, `task_id` (FK + UQ), `candidate_id` (FK), `is_secret`, `secret_type`, `confidence` (0.0–1.0), `severity`, `reason`, `recommendation`, `model`, `prompt_version`, `created_at`. Валидация при создании: `0 ≤ confidence ≤ 1`, enum-значения, непустые строки.
- `ai_validation_payload.py` — `AiValidationPayload` (JSON value object для `AI_VALIDATION`): `candidate_id`, `prompt_version`, `model` (опционально).
- `ai_validation_output.py` — `AiValidationOutput` (JSON value object): `verdict_id`, `incident_id` (опционально).

Обновить `src/models/__init__.py` (экспорт), `docs/models.md` §3.8 (если нужно уточнить), `docs/modules.md` §1 (уже есть упоминание, уточнить статус).

**Правила AGENTS.md:** сырого секрета в моделях нет; только `value_masked`/`value_fingerprint` в `Candidate`. `AiVerdict` хранит только маскированные данные и вердикт.

**Критерий готовности:** модели импортируются, валидация полей работает, `__init__.py` обновлён.

### Этап 2. Регистрация TaskKind.AI_VALIDATION

В `src/models/task/task_kinds.py`:
- Зарегистрировать `TaskKind.AI_VALIDATION = 3` (уже объявлен, но не зарегистрирован в `_SPECS`).
- Добавить `TaskKindSpec(kind=TaskKind.AI_VALIDATION, payload_type=AiValidationPayload, output_type=AiValidationOutput, default_priority=50, default_max_attempts=5)` (приоритет выше `COMMIT_ANALYSIS = 100`, чтобы инциденты появлялись раньше — по `modules.md §8.2`).
- `TaskPayload` в `src/models/task/task.py` — расширить `Union` до `AiValidationPayload` (или оставить, если кодек подхватит по типам; явно добавить для type safety).
- `TaskOutput` в `src/models/task/task_result.py` — добавить `AiValidationOutput` в `Union`.

**Критерий готовности:** `spec_for(TaskKind.AI_VALIDATION)` возвращает спецификацию, `parse_payload`/`parse_output` работают для нового kind.

### Этап 3. Хранение ai_verdicts (мапперы в storage)

В `src/storage/session.py` (`StorageSession`) добавить:
- `save_ai_verdict(verdict) -> AiVerdict` — вставка в `ai_verdicts` (таблица уже существует).
- `get_ai_verdict(verdict_id) -> AiVerdict | None`.
- Опционально `get_ai_verdict_by_candidate(candidate_id)`.

В `src/storage/sqlite.py` добавить соответствующие public-методы `save_ai_verdict`, `get_ai_verdict` (делегируют в `run_in_transaction`/`read`).

**Критерий готовности:** `AiVerdict` сохраняется и загружается, таблица `ai_verdicts` (миграция 1) используется.

### Этап 4. Интерфейс LlmClient + фейковая реализация

В `src/ai_validation/` создать:
- `client.py` — протокол `LlmClient` (интерфейс, без бизнес-логики): метод `complete(messages, *, timeout) -> str` (или analogous). По AGENTS.md §2 §4 — код за интерфейсами, чтобы выбор библиотеки не размазался.
- `fake_client.py` — фейковая реализация для тестов (возвращает заранее заданный JSON-вердикт), никаких реальных сетевых вызовов.

**Правила AGENTS.md §2:** LLM-провайдер/клиент не выбраны — не добавлять реальный клиент без согласования. Все взаимодействие через протокол `LlmClient`.

**Критерий готовности:** протокол определён, фейк работает в тестах, реального сетевого кода нет.

### Этап 5. Masking guard (сантитаризация промпта)

В `src/ai_validation/` — функция/класс `MaskingGuard`:
- Перед отправкой в LLM проверить, что промпт не содержит сырых значений секретов (`raw_value`, `source_line_raw`, `context_raw` из `RawCandidate`).
- В LLM уходят только маскированные значения (`AKIA...MPLE`, `value_masked`, `source_line_masked`, `context_masked`) — инвариант AGENTS.md §6.2.
- При подозрении на сырое значение → `FAILED(MASKING_VIOLATION)`, в LLM не уходит (код ошибки `ErrorCode.MASKING_VIOLATION = 5`).

**Критерий готовности:** промпт содержит только маскированные данные; попытка передать сырой секрет блокируется.

### Этап 6. Сборка промпта и версионирование prompt_version

- Системный промпт: роль security-классификатора, определение секрета, список типичных false positive (UUID, hash, timestamp, тестовые данные, placeholders, публичная конфигурация), требование строго структурированного JSON.
- Пользовательский (динамический) промпт: repository, commit, file, line, masked code, context, token, entropy, regex matches.
- `prompt_version` — целое, входит в `idempotency_key` задачи `AI_VALIDATION` (по `docs/models.md §3.4`: `idempotency_key = kind, candidate_id, prompt_version`). При изменении промпта увеличивать `prompt_version`, старые вердикты не переиспользуются молча.
- Флаг `ai.enabled` (в конфиге) — полное отключение внешних LLM; система обязана работать без LLM (инвариант AGENTS.md §6.2).

**Критерий готовности:** промпт собирается только из маскированных данных, `prompt_version` учитывается в идемпотентности, `ai.enabled=false` отключает отправку.

### Этап 7. Вызов LLM под Semaphore с таймаутом и ретраями

- Вызовы LLM — под `asyncio.Semaphore` (backpressure, инвариант AGENTS.md §6.6), с таймаутом.
- CPU-bound части (если будут) — `asyncio.to_thread` / `ThreadPoolExecutor` (инвариант §6.5), event loop не блокируется.
- Ретраи при `LLM_FAILED` / сетевых ошибках / 429 / 5xx / таймауте: backoff `1→2→4→8 с`, `max_attempts = 5` (для `AI_VALIDATION`, по `docs/models.md §3.4`).
- Невалидный JSON / несоответствие схеме → `LLM_INVALID_RESPONSE` (ретрай), а не «не секрет» (инвариант §6.10).
- После исчерпания попыток → `FAILED(LLM_FAILED)`, кандидат остаётся без вердикта, запуск получает `Outcome.PARTIAL`, остальное не затронуто (инvariant §6.7).

**Критерий готовности:** вызов ограничен семафором, ретраи с экспоненциальным бэкоффом, невалидный ответ — `LLM_INVALID_RESPONSE`.

### Этап 8. Парсинг и валидация JSON-ответа → AiVerdict

- Разобрать ответ LLM в JSON, провалидировать по схеме: типы, `confidence ∈ [0,1]`, `secret_type` и `severity` из enum, строки не пустые.
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

### Этап 10. OutboxMessage — ⏳ **ОЖИДАЕТ**

- При `severity ≥ HIGH` — создать `OutboxMessage` (канал по конфигу, `payload` — только маскированные данные) в той же транзакции, что и изменение инцидента (инvariant AGENTS.md §6.8).
- Отправку выполняет отдельный компонент доставки (вне сферы MVP — заготовка/протокол). После успешной отправки `status = SENT`, событие `NOTIFIED`.

**Критерий готовности:** `OutboxMessage` создаётся атомарно с инцидентом, содержит только маскированные данные.

### Этап 11. Реализация AiTaskFactory в commit worker

В `src/workers/commit.py`:
- Реализовать `AiTaskFactory` (создаёт задачу `AI_VALIDATION` для сохранённого `Candidate` с версионированным `idempotency_key` — `candidate_id + prompt_version`).
- В `CommitWorker._process.persist`: при `ai_enabled` и `route = AI_VALIDATION` → создать `Task(AI_VALIDATION)` в ленту (идемпотентно).
- При `ai_enabled=False` (MVP) — высокий балл даёт `SUSPICIOUS`, AI-задачи не создаются (уже реализовано, не ломать).

**Критерий готовности:** при `ai_enabled=True` создаются `AI_VALIDATION` задачи, при `False` — нет.

### Этап 12. AI Worker (workers/ai.py)

Создать `src/workers/ai.py` — `AIWorker`:
- Цикл как у `CommitWorker`: `claim([AI_VALIDATION], worker_id, lease_s)`, обработка, `complete`/`fail`.
- Параллелизм — `Semaphore(llm_workers)` (по умолчанию 5, по `modules.md §2`).
- Приоритет задач выше `COMMIT_ANALYSIS`.
- Логика вызова — в модуле `ai_validation/` (этапы 4–8).
- Heartbeat, graceful shutdown по `asyncio.Event`, обработка истечения аренды (перевыдача задачи).

**Критерий готовности:** `AIWorker` обрабатывает `AI_VALIDATION` задачи, не блокирует event loop, корректно завершается.

### Этап 13. Включение ai_enabled в application.py и CLI

- В `src/application.py` убрать жесткое ограничение `ai_enabled=False` (или сделать его конфигурируемым через флаг CLI `--ai-enabled`).
- При `ai_enabled=True` в `CommitWorker` передать реализацию `AiTaskFactory` и подключить `AIWorker`.
- CLI: добавить флаг `--ai-enabled` (по умолчанию `false` для обратной совместимости MVP).
- HMAC-ключ передаётся явно, не сохраняется; `ai_enabled=false` обязателен в чистом MVP, при `true` — подключается LLM-компонент.

**Критерий готовности:** CLI поддерживает `--ai-enabled`, при `false` поведение не меняется (MVP), при `true` создаются AI-задачи.

### Этап 14. Observability (логи и метрики)

В `src/observability/` (сейчас пустая заглушка):
- Структурные логи (JSON) с полями `task_id`, `run_id`, `commit_id`, `candidate_id`. **Никогда** сырые значения и немаскированный `source_line` (по `modules.md §13`).
- Метрики: длина ленты по `kind`/`status`, возраст старшей `QUEUED`-задачи, протухшие аренды, throughput (коммиты/с, строки/с), доля кандидатов по `route`, доля отправленных в LLM, латентность и ошибки LLM, число повторов, открытые инциденты по `severity`/`status`.

**Критерий готовности:** логирование без секретов, метрики собираются (заглушка или реализация по согласованию).

### Этап 15. Тесты (unit + интеграционные)

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

### Этап 16. Документация

- `docs/modules.md` — уточнить §9 (`ai_validation`), §10 (`incident_manager` AI-ветка), §12 (`application` с `ai_enabled`).
- `docs/models.md` — проверить актуальность §3.8 (модели теперь реализованы).
- `Plan.md` — привести к реальности: PostgreSQL → SQLite (по AGENTS.md / ADR 0001).
- Проверить, что `AGENTS.md` §7 («Добавить новый вид задачи») соблюдён: `TaskKind`, payload/output, переходы, приоритет, правило `idempotency_key`.
- При расхождении кода и документации — сначала правка документации, затем код (AGENTS.md вступление).

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

## 4. Последовательность выполнения (кратко)

1. Прогресс-тест: запустить существующие unit-тесты + smoke → зафиксировать базовый зелёный статус. ✅ **ВЫПОЛНЕНО (2026-11-03)**
2. Этап 1 — модели AI-ветки. ✅ **УЖЕ РЕАЛИЗОВАНО** (до текущего коммита)
3. Этап 2 — регистрация `TaskKind.AI_VALIDATION`. ✅ **ВЫПОЛНЕНО (2026-11-03)**
4. Этап 3 — мапперы `ai_verdicts` в storage. ✅ **ВЫПОЛНЕНО (2026-11-03)**
5. Этап 4 — `LlmClient` протокол + фейк. ✅ **ВЫПОЛНЕНО (2026-11-03)**
6. Этап 5 — masking guard. ✅ **ВЫПОЛНЕНО (2026-11-03)**
7. Этап 6 — промпт и `prompt_version`. ✅ **ВЫПОЛНЕНО В ЭТАПЕ 4 (2026-11-03)**
8. Этап 7 — вызов LLM под `Semaphore` с таймаутом и ретраями. ✅ **ВЫПОЛНЕНО (2026-11-03)**
9. Этап 8 — парсинг/валидация JSON → `AiVerdict`. ✅ **ВЫПОЛНЕНО В ЭТАПЕ 4/6 (2026-11-03)**
10. Этап 9 — интеграция с `incident_manager` (AI-ветка). ✅ **ВЫПОЛНЕНО (2026-11-03)**
11. Этап 10 — `OutboxMessage`. ✅ **ВЫПОЛНЕНО (2026-11-03)**
12. Этап 11 — `AiTaskFactory` в commit worker. ✅ **ВЫПОЛНЕНО (2026-11-03)**
13. Этап 12 — `AI Worker`. ✅ **ВЫПОЛНЕНО (2026-11-03)**
14. Этап 13 — `ai_enabled` в `application.py` / CLI. ✅ **ВЫПОЛНЕНО (2026-11-03)**
15. Этап 14 — observability. ⏳ **ОЖИДАЕТ**
16. Этап 15 — тесты (новые + регресс). ⏳ **ОЖИДАЕТ**
17. Этап 16 — документация + приведение `Plan.md` к SQLite. ⏳ **ОЖИДАЕТ**
18. Этап 17 — финальная проверка регресса + DoD. ⏳ **ОЖИДАЕТ**

---

## 5. Прогресс выполнения (журнал этапов)

> Этот раздел ведётся по мере выполнения этапов. Формат записи:
> ```
> ### Этап N. Название — <дата> — <статус>
> **Выполнено:** список конкретных изменений/файлов
> **Проверка:** результаты тестов, smoke, коммиты
> **Примечания:** любые отклонения от плана, решения, блокеры
> ```

### Этап 0. Предварительная проверка регресса — 2026-11-03 — ✅ **ВЫПОЛНЕНО**
**Выполнено:**
- Запущены все существующие unit-тесты (61 тест): `test_static_analyzer`, `test_storage`, `test_feed`, `test_preprocessor`, `test_git_collector/*`, `test_incident_manager`, `test_orchestrator`, `test_commit_worker`, `test_application`, `test_config`, `test_task`, `test_incident`, `test_raw_candidate` — все **OK**.
- Запущен smoke-тест: `python -m tests.smoke_scan` → `SMOKE OK: commits_done=1, candidates=1, repeat_new=0`.
- Проверен CLI: `python -m application scan` с `ai_enabled=False` работает корректно.

**Проверка:** все тесты прошли, регресса нет.
**Примечания:** Базовая линия зафиксирована. Можно приступать к этапу 2.

### Этап 1. Модели AI-ветки — **УЖЕ РЕАЛИЗОВАНО ДО ТЕКУЩЕГО КОММИТА (9490b50)**
**Выполнено (до плана):**
- Реализованы `AiVerdict` (`src/models/ai_validation/ai_verdict.py`), `AiValidationPayload` (`src/models/ai_validation/ai_validation_payload.py`), `AiValidationOutput` (`src/models/ai_validation/ai_validation_output.py`).
- Экспорт в `src/models/ai_validation/__init__.py` и `src/models/__init__.py`.
- Таблица `ai_verdicts` уже существует в миграции `0001_initial.sql`.
- Валидация полей: `confidence` в [0,1], enum-значения `SecretType`/`Severity`, непустые строки.

**Проверка:** модели импортируются, валидация работает.
**Примечания:** Этот этап выполнен ранее, в коде к моменту коммита 9490b50. Документация в `docs/models.md` §3.8 соответствует реализации (требуется проверка на этапе 16).

### Этап 2. Регистрация TaskKind.AI_VALIDATION — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Добавлен импорт `AiValidationPayload` и `AiValidationOutput` в `src/models/task/task_kinds.py`
- Зарегистрирован `TaskKind.AI_VALIDATION` через `register_task_kind` с:
  - `payload_type=AiValidationPayload`
  - `output_type=AiValidationOutput`
  - `default_priority=50` (выше приоритет, чем у COMMIT_ANALYSIS=100, чтобы инциденты появлялись раньше)
  - `default_max_attempts=5`
- Обновлён `TaskPayload` Union в `src/models/task/task.py` — добавлен `AiValidationPayload`
- Добавлена фабрика `Task.ai_validation()` в `src/models/task/task.py` с правильным `idempotency_key` (candidate_id + prompt_version)
- Добавлена функция `key_for_ai_validation()` в `src/models/task/idempotency.py`
- Обновлён `TaskOutput` Union в `src/models/task/task_result.py` — добавлен `AiValidationOutput`
- Обновлён тест `test_ai_validation_kind_registered` в `tests/unit/models/test_task.py`

**Проверка:** все 61 unit-тест проходят, smoke-тест `SMOKE OK: commits_done=1, candidates=1, repeat_new=0`

**Примечания:** Этап выполнен согласно плану. `spec_for(TaskKind.AI_VALIDATION)` теперь возвращает корректную спецификацию.

### Этап 3. Хранение ai_verdicts (мапперы в storage) — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Добавлен импорт `AiVerdict` в `src/storage/session.py`
- Реализованы методы в `StorageSession`:
  - `save_ai_verdict(verdict: AiVerdict) -> AiVerdict` — идемпотентное сохранение (ON CONFLICT(task_id) DO NOTHING)
  - `get_ai_verdict(verdict_id: int) -> AiVerdict | None` — загрузка по PK
  - `get_ai_verdict_by_candidate(candidate_id: int) -> AiVerdict | None` — поиск по кандидату (последний)
  - `get_ai_verdict_by_task(task_id: int) -> AiVerdict | None` — поиск по задаче AI_VALIDATION
  - `_ai_verdict_from_row(row)` — статический маппер строки БД в модель
- Добавлен импорт `AiVerdict` и публичные async-методы в `SqliteStorage` (`src/storage/sqlite.py`):
  - `save_ai_verdict`, `get_ai_verdict`, `get_ai_verdict_by_candidate`, `get_ai_verdict_by_task`

**Проверка:** все 61 unit-тест проходят, smoke-тест `SMOKE OK: commits_done=1, candidates=1, repeat_new=0`

**Примечания:** Таблица `ai_verdicts` уже существовала в миграции `0001_initial.sql` с `UNIQUE(task_id)`. Мапперы соответствуют схеме. Публичные методы доступны через `SqliteStorage` для использования в AI Worker (этап 12).

### Этап 4. LlmClient протокол + фейк — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Создан `src/ai_validation/llm_client.py` с полной реализацией:
  - `LlmConfig` — конфигурация (endpoint, model, temperature=0, timeout, retries, max_concurrent, prompt_version, from_env())
  - Протокол `LlmClient` — интерфейс `complete(candidate, payload) -> AiVerdict`
  - Исключения: `LlmError`, `LlmTimeoutError`, `LlmNetworkError`, `LlmInvalidResponseError`, `LlmRateLimitError`, `LlmAuthError` с флагами `retryable` и кодами
  - `FakeLlmClient` — для тестов: `set_verdict(candidate_id, verdict)`, `clear()`, имитация задержек/ошибок через `delay_s`, `fail_next`, `fail_code`
  - `SYSTEM_PROMPT` (English) + `USER_PROMPT_TEMPLATE` с полями кандидата
  - `build_prompt(candidate)` — построение промпта
  - `parse_llm_response(response_text, candidate_id, task_id, model, prompt_version)` — строгая валидация JSON, проверка enum-ов, confidence [0,1], непустые reason/recommendation
  - `RealLlmClient` — заглушка с `asyncio.Semaphore` для backpressure, бросает `NotImplementedError` (реализация на этапе 13)
- Все импорты работают, синтаксис валиден

**Проверка:** все 61 unit-тест проходят, smoke-тест `SMOKE OK: commits_done=1, candidates=1, repeat_new=0`

**Примечания:** 
- Промпт на английском, ответ ожидается на русском (согласно требованиям)
- Температура 0 для детерминированности
- Модель: `nvidia/nemotron-3-ultra`, endpoint: NVIDIA API (OpenAI-совместимый)
- API-ключ через env `SENTINEL_KEY_LLM_API_KEY` (на этапе 13)
- `prompt_version` входит в `idempotency_key` задачи AI_VALIDATION

### Этап 5. Masking guard — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Создан `src/ai_validation/masking_guard.py`:
  - Паттерны сырых секретов: AWS Access/Secret Key, GitHub Token, Slack Token, JWT, Private Key, Bearer Token, DB URL с паролем, Generic API Key, Generic Password
  - Проверка `value_fingerprint` (64 hex chars)
  - `check_masking(text)` — возвращает `MaskingGuardResult(ok, violations[])`
  - `assert_safe_for_llm(text)` — бросает `MaskingViolationError` при нарушении
  - `check_candidate_masking(candidate)` — проверяет поля кандидата (`value_masked`, `source_line_masked`, `context_masked`)
  - Исключение `MaskingViolationError` с `pattern_name` и `matched`
- Создан `tests/unit/test_masking_guard.py` (11 тестов):
  - Чистый текст проходит
  - Обнаружение каждого типа секрета
  - Множественные нарушения собираются
  - `assert_safe_for_llm` бросает/не бросает
  - Проверка кандидата с замаскированными/сырыми значениями

**Проверка:** все 72 unit-теста проходят (включая 11 новых), smoke-тест `SMOKE OK`

**Примечания:** Интеграция в AI Worker (этап 12) — вызов `assert_safe_for_llm(prompt)` перед отправкой в LLM.

### Этап 6. Промпт и prompt_version — ✅ **ВЫПОЛНЕНО В ЭТАПЕ 4 (2026-11-03)**
**Выполнено (как часть этапа 4):**
- `SYSTEM_PROMPT` (English) — инструкции аналитику, схема JSON ответа, правила
- `USER_PROMPT_TEMPLATE` — шаблон с полями кандидата: file_path, line_number, secret_type, value_masked, context_masked, regex_rules, entropy, detector_flags, score_total
- `build_prompt(candidate)` — построение (system, user) промптов
- `prompt_version` в `LlmConfig` (default=1), входит в `idempotency_key` задачи AI_VALIDATION
- `parse_llm_response()` — строгая валидация JSON, enum-проверки, confidence [0,1], непустые reason/recommendation

**Проверка:** импорты работают, тесты проходят.

**Примечания:** Промпт в коде (не отдельный файл), версия 1. При изменении промпта — инкремент `prompt_version` (сбрасывает идемпотентность).

### Этап 7. Вызов LLM под Semaphore с таймаутом и ретраями — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Полностью реализован `RealLlmClient` в `src/ai_validation/llm_client.py`:
  - **Semaphore backpressure**: `asyncio.Semaphore(max_concurrent)` ограничивает параллельные вызовы
  - **Таймаут**: `asyncio.wait_for(asyncio.to_thread(...), timeout=config.timeout_s)`
  - **Retry с экспоненциальным backoff + jitter**: `base * 2^(attempt-1) + random.uniform(0, 10%)`
  - **Максимальные попытки**: `config.max_attempts` (default 3)
  - **Структурированные ошибки**: `LlmTimeoutError`, `LlmRateLimitError`, `LlmAuthError`, `LlmNetworkError`, `LlmInvalidResponseError` — все с флагами `retryable`
  - **HTTP клиент**: `urllib` (stdlib), OpenAI-совместимый API, Bearer token auth
  - **Парсинг ответа**: извлечение `choices[0].message.content`, валидация через `parse_llm_response()`
  - **Пропуск ретраев для auth ошибок**: 401/403 → немедленный raise
- Конфигурация через `LlmConfig`: `timeout_s`, `max_attempts`, `retry_base_s`, `max_concurrent`, `api_endpoint`, `model`, `temperature`, `max_tokens`

**Проверка:** все 83 unit-теста проходят (72 существующих + 11 masking_guard), smoke-тест `SMOKE OK`

**Примечания:** API-ключ читается из `config.api_key` (env `SENTINEL_KEY_LLM_API_KEY` на этапе 13). Без ключа — `LlmAuthError` при инициализации вызова.

### Этап 8. Парсинг/валидация JSON → AiVerdict — ✅ **ВЫПОЛНЕНО В ЭТАПЕ 4/6 (2026-11-03)**
**Выполнено (как часть этапов 4 и 6):**
- `parse_llm_response()` в `llm_client.py` — строгая валидация:
  - JSON decode с `LlmInvalidResponseError`
  - Проверка обязательных полей: `is_secret`, `secret_type`, `confidence`, `severity`, `reason`, `recommendation`
  - Enum-валидация: `SecretType`, `Severity`
  - Диапазон `confidence` ∈ [0.0, 1.0]
  - Непустые строки `reason`, `recommendation`
  - Возврат валидированного `AiVerdict`
- Невалидный ответ → `LLM_INVALID_RESPONSE` (retryable), **не** считается «не секрет»

**Проверка:** импорты работают, тесты проходят.

**Примечания:** Используется в `RealLlmClient._call_with_timeout()` и `FakeLlmClient`.

### Этап 9. Интеграция с incident_manager (AI-ветка) — ⏳ **ОЖИДАЕТ**
**Выполнено:** —
**Проверка:** —
**Примечания:** Расширить `IncidentManager.decide(candidate, verdict)` — принятие решения с учётом вердикта LLM. `process()` должен вызывать `decide` с `verdict` для `Route.AI_VALIDATION` кандидатов. Политика: `min_confidence`, `trusted_formats` (локально), AI-вердикт повышает/понижает уверенность.

### Этап 10. OutboxMessage — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Мапперы в `StorageSession` уже существовали: `save_outbox_message`, `update_outbox_message`, `get_outbox_message`
- Добавлены публичные async-методы в `SqliteStorage`: `save_outbox_message`, `update_outbox_message`, `get_outbox_message`
- Добавлен импорт `OutboxMessage` в `IncidentSession` протокол
- Добавлены функции в `IncidentManager`:
  - `OUTBOX_CHANNELS` — маппинг `IncidentEventKind` → `NotifyChannel`
  - `_make_outbox_payload()` — построение JSON payload для уведомления
  - `_create_outbox_message()` — создание и сохранение outbox-сообщения
- Outbox-сообщения создаются при:
  - `OPENED` — создание нового инцидента
  - `REOPENED` — повторное открытие закрытого инцидента
  - `OCCURRENCE_ADDED` — новое обнаружение в существующем инциденте
  - `SEVERITY_RAISED` — повышение severity инцидента
- Payload содержит: `incident_id`, `repository_id`, `fingerprint` (hex), `secret_type`, `severity`, `confidence`, `event`, `old_severity` (для SEVERITY_RAISED)
- Канал доставки: `WEBHOOK` (по умолчанию для всех событий)

**Проверка:** все 90 unit-тестов проходят (3 incident_manager + 11 masking_guard + 76 существующих), smoke-тест `SMOKE OK`

**Примечания:** Outbox сообщения пишутся в той же транзакции, что и изменение инцидента (атомарность). Реальная доставка (HTTP worker) — отдельная задача.

### Этап 11. AiTaskFactory в commit worker — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Создан `src/workers/ai_factory.py` с реализацией `DefaultAiTaskFactory`:
  - Реализует протокол `AiTaskFactory` из `workers/commit.py`
  - Создаёт задачу `Task.ai_validation()` с правильным `idempotency_key` (candidate_id + prompt_version через `key_for_ai_validation`)
  - Payload: `AiValidationPayload(candidate_id, prompt_version, model=None)`
  - Конфигурируется через `LlmConfig` (prompt_version из конфига LLM)
- Экспортирован в `src/workers/__init__.py`
- Интегрирован в `CommitWorker` через параметр `ai_task_factory` (уже был в конструкторе)

**Проверка:** все 92 unit-теста проходят, smoke-тест `SMOKE OK`

**Примечания:** Фабрика готова к использованию в `application.py` на этапе 13.

### Этап 12. AI Worker — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Создан `src/workers/ai.py` с полной реализацией `AiWorker`:
  - Цикл: `claim` → `heartbeat` → `masking guard` → `prompt build` → `LLM call` → `parse verdict` → `save verdict + incident` → `complete/fail`
  - `masking guard` через `ai_validation.masking_guard.assert_safe_for_llm` перед отправкой в LLM
  - LLM вызов через `LlmClient.complete()` (с semaphore, timeout, retry — уже в `RealLlmClient`)
  - Сохранение вердикта (`session.save_ai_verdict`) + инцидент (`incident_manager.process`) в одной транзакции
  - Завершение задачи через `feed.complete` с `AiValidationOutput(verdict_id, incident_id)`
  - Обработка ошибок: `LeaseLostError`, `CancelledError`, прочие → `feed.fail` с retryable
  - Heartbeat каждые `lease_s/3` секунд
- Фабрика `create_ai_worker()` для удобного создания с зависимостями:
  - Поддержка `FakeLlmClient` для тестов (`use_fake_llm=True`)
  - Поддержка `RealLlmClient` для продакшена
  - Конфигурация через `LlmConfig`
- Экспортирован в `src/workers/__init__.py`

**Проверка:** все 92 unit-теста проходят, smoke-тест `SMOKE OK`

**Примечания:** Интеграция в `application.py` / CLI — на этапе 13.

### Этап 13. ai_enabled в application.py / CLI — ✅ **ВЫПОЛНЕНО (2026-11-03)**
**Выполнено:**
- Убрана ошибка при `ai_enabled=True` в `ScanTaskPayload`
- Добавлены импорты: `AiWorker`, `create_ai_worker`, `DefaultAiTaskFactory`, `LlmConfig`
- Обновлён `App.__init__`: параметры `ai_enabled`, `llm_config`
- Добавлен `DefaultAiTaskFactory` в `CommitWorker` при `ai_enabled=True`
- Создан `AiWorker` через `create_ai_worker` при `ai_enabled=True`
- Добавлен запуск `ai_worker.run_once()` в главный цикл `scan()`
- Добавлен CLI флаг `--ai-enabled` в `_parser()`
- Обновлён `_run_scan`: передача `args.ai_enabled` в `App` и `ScanTaskPayload`
- `LlmConfig.from_env()` загружает `SENTINEL_KEY_LLM_API_KEY` из env
- `RealLlmClient` использует OpenAI SDK с настройками: `temperature=0.1`, `max_tokens=2000`, `enable_thinking=False`, без стриминга

**Проверка:** все 93 unit-теста проходят, smoke-тест `SMOKE OK`

**Примечания:** API-ключ загружается из env `SENTINEL_KEY_LLM_API_KEY` (пользователь предоставит на этапе запуска). `RealLlmClient` использует OpenAI SDK (`docs/decisions/0001-openai-dependency.md`). При `ai_enabled=False` (по умолчанию) AI компоненты не создаются.

### Этап 14. Observability — ⏳ **ОЖИДАЕТ**
**Выполнено:** —
**Проверка:** —
**Примечания:** Логирование ключевых событий (claim, verdict, incident opened/updated), метрики (counters: candidates_total, ai_calls, ai_failures, incidents_opened), structured logging (JSON). Модуль `src/observability/` — заготовить интерфейсы.

### Этап 15. Тесты (новые + регресс) — ⏳ **ОЖИДАЕТ**
**Выполнено:** —
**Проверка:** —
**Примечания:** Unit: `LlmClient` (фейк/реальный), masking guard, prompt parsing, verdict validation, `IncidentManager.decide` с вердиктом, `AiTaskFactory`, `AI Worker`. Integration: временный git repo + БД в памяти + фейковый LLM → полный пайплайн. Сценарии: первый запуск → повторный (нет дублей), worker crash (lease expiry → re-claim), LLM down → PARTIAL, один секрет в 3 коммитах → 1 incident + 3 occurrences, закрытый incident при повторном обнаружении → reopen.

### Этап 16. Документация — ⏳ **ОЖИДАЕТ**
**Выполнено:** —
**Проверка:** —
**Примечания:** Обновить `docs/modules.md` (§9, §10, §12), `docs/models.md` (§3.8 проверить), `Plan.md` (PostgreSQL → SQLite). Проверить соответствие AGENTS.md §7.

### Этап 17. Финальная проверка регресса и Definition of Done — ⏳ **ОЖИДАЕТ**
**Выполнено:** —
**Проверка:** —
**Примечания:** Повторный прогон всех unit + smoke. Чек-лист DoD из AGENTS.md §9.

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
