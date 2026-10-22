# Модули и логические компоненты sentinel_key

Исходники — `sentinel_key/src/`, каждый модуль — отдельная папка. Модели и их поля описаны в [`models.md`](models.md); здесь — кто что делает и как. Названия моделей даны в `Code`-формате и соответствуют `models.md`.

## 0. Карта модулей

```
sentinel_key/src/
  application.py      # точка сборки MVP и CLI для локального репозитория
  models/            # доменные классы (один файл — одна модель)
    common/          # clock, JSON codec
    repository/      # Repository, Commit, CommitParent
    task/            # Task, TaskResult, scan/commit payloads
    candidate/       # RawCandidate, Candidate, ScoreBreakdown
    incident/        # Incident, Occurrence, IncidentEvent, OutboxMessage
  config/            # конфигурационные модели и загрузка конфигурации
  git_collector/     # получение истории и diff из Git
  preprocessor/      # diff → добавленные строки → RawCandidate
  static_analyzer/   # regex, энтропия, контекст, scoring, маршрутизация
  feed/              # лента задач
  orchestrator/      # запуск анализа: план, fan-out, завершение
  workers/           # демоны: commit worker, ai worker
  ai_validation/     # маскирование, prompt, LLM, разбор вердикта
  incident_manager/  # решение об инциденте, дедупликация, жизненный цикл
  storage/           # единственное место с SQL, транзакции
  observability/     # логи и метрики
```

```mermaid
flowchart LR
    ORCH[orchestrator] --> FEED[(feed + storage)]
    FEED --> CW[commit worker]
    CW --> GIT[git_collector]
    CW --> PRE[preprocessor] --> STA[static_analyzer]
    STA -->|Candidate + AI-задачи| FEED
    FEED --> AW[ai worker]
    AW --> AI[ai_validation]
    AW --> INC[incident_manager]
    CW --> INC
    INC --> FEED
```

**Правила зависимостей.** Поток данных идёт слева направо; модули обмениваются только классами из `models/`. SQL — только в `storage/`. `static_analyzer` и `preprocessor` — чистые функции без обращения к БД и сети.

---

## 1. `models/`

**Назначение.** Доменные классы, сгруппированные по функциональности: `repository`, `task`, `candidate`, `incident` (+ `common` для утилит). Общие enum — `SecretType`, `Severity`, `DetectorFlags` в `models/enums.py`. Публичный импорт — через `models` (пакет реэкспортирует классы).

**Как устроен.**
- Один файл — одна модель внутри подпакета; enum, принадлежащий только этой модели, лежит в её файле.
- В моделях живёт только **поведение, зависящее от самой модели**: проверка переходов (`Task.transition`, `Incident.set_status`), расчёт `ScoreBreakdown.total`, `Task.schedule_retry`. Ни SQL, ни сети, ни чтения конфигурации.
- `TaskKind.AI_VALIDATION` зарезервирован (значение 3). Классы `AiVerdict`, `AiValidationPayload`, `AiValidationOutput` и регистрация kind — зона ответственности трека `ai_validation` (пока отсутствуют).
- Любое изменение полей начинается с `docs/models.md`.

---

## 2. `config/`

**Назначение.** Загрузка и валидация конфигурации; классы `ScoringConfig` и `IncidentPolicy`, а также настройки воркеров и лимитов.

**Что конфигурируется (всё имеет значение по умолчанию):**
- scoring: веса и пороги `low`/`high`;
- политика инцидентов: `min_ai_confidence`, `trusted_formats`, `min_local_score`, `bump_if_in_head`;
- размеры очередей worker'а (по умолчанию 100/500), `batch_size` (50);
- число worker'ов: git (4), cpu-потоки (4), llm (5);
- `lease_s`, `max_attempts`, базовая задержка backoff;
- `ai.enabled`, `ai.prompt_version`, таймаут LLM;
- лимиты: `max_file_bytes`, `max_line_len`, список игнорируемых путей и расширений.

`PreprocessorConfig` задаёт `context_lines = 2`, `max_line_len = 4096`,
`min_bare_token_len = 20`, `mask_visible_chars = 4` и наборы игнорируемых
каталогов, lock- и minified-файлов. HMAC-ключ не является полем
конфига: его байты передаются в `Preprocessor` точкой сборки и не
сохраняются в моделях.

**Принцип:** пороги и коэффициенты не зашиваются в код; конфигурация неизменяема после старта.

---

## 3. `git_collector/` — Git Collector

**Назначение.** Единственный модуль, достающий историю и diff из VCS. Отдаёт данные, ничего не интерпретирует. Подробности контракта и бэкендов — в [`git_collector.md`](git_collector.md).

**Интерфейс.** Протокол `GitCollector` (репозиторий привязан в конструкторе):
- `list_commits(refs) -> list[CommitInfo]` — SHA (`bytes`), родители, `committed_at`, топологический порядок от старых к новым;
- `get_diff(sha) -> AsyncIterator[FileDiff]` — потоковый разбор diff по файлам;
- `file_contains_in_head(path, line_matcher, *, ref=None) -> bool` — проверка `still_in_head`.

Фабрика `create_git_collector(location, kind=...)`: `LocalGitCollector` (реализован), `GitHubCollector` / `GitLabCollector` (заготовки: mirror + делегирование в local).

**Как работает (local)**
- Процессы — `asyncio.create_subprocess_exec` (без shell), с таймаутом; SHA валидируется до передачи в argv.
- Diff читается потоком; бинарные файлы и файлы больше `max_file_bytes` → `FileDiff` с `skip_reason`.
- Merge-коммиты: diff относительно первого родителя.
- Параллелизм — `Semaphore(git_workers)` на инстанс.

**Ошибки.** Недоступен репозиторий/refs → `GitCollectorError` (`ErrorCode.GIT_ERROR`). Ошибка по одному SHA → коммит `FAILED` (решает worker). Ошибка/skip по файлу → `DIFF_ERROR` / `skip_reason`, остальные файлы продолжаются.

---

## 4. `preprocessor/` — модуль предобработки операции

**Назначение.** Из diff коммита получает **добавленные строки** и выделяет из них **потенциальные секретные значения** — `RawCandidate`.

**Вход:** `FileDiff` и `commit_id`. **Выход:** поток `RawCandidate` (содержит сырое значение только в памяти).

**Как работает**
1. **Парсинг diff.** Берутся только строки `+`. Номер строки вычисляется по hunk-заголовкам `@@ -a,b +c,d @@`. Для каждой строки сохраняется контекст ±N строк (включая неизменённые).
2. **Фильтрация.** Пропускаются lock-файлы, vendor, minified-файлы, бинарные файлы, строки длиннее `max_line_len` (список — в конфиге).
3. **Извлечение значений.** Задача — не разбить строку на слова, а выделить структуры:
   - `KEY = "value"`, `key: value`, `export KEY=value` → `key_name`, `raw_value`;
   - `Authorization: Bearer …` → `key_name=Authorization`, `secret_type_hint=BEARER_TOKEN`;
   - URL с credentials (`postgres://user:pass@host`) → отдельное значение пароля, `DATABASE_URL`;
   - PEM-блок `-----BEGIN … PRIVATE KEY-----` → многострочный кандидат, `PRIVATE_KEY`;
   - «голые» длинные токены в строках и комментариях.
4. **Сразу после извлечения** считаются `value_fingerprint` (HMAC-SHA256, ключ из окружения) и `value_masked`; строка и контекст маскируются для будущего `Candidate`.
5. **Дедупликация в пачке** по `(file_path, line_number, value_fingerprint)`.

**Не делает:** не считает риск, не обращается к БД и LLM, не логирует сырые значения.

**Реализация.** `Preprocessor` принимает HMAC-ключ явно, обрабатывает
готовый `FileDiff` и возвращает итератор `RawCandidate`. Так модуль остаётся
чистым: не читает environment сам и не скрывает глобальное состояние.

**Тестовые сценарии:** несколько hunk'ов, переименование файла, `\ No newline at end of file`, CRLF, merge-коммит, многострочный PEM.

---

## 5. `static_analyzer/` — статический анализатор

**Назначение.** По `RawCandidate` собирает независимые признаки (regex, энтропия, контекст), считает `ScoreBreakdown`, выставляет `DetectorFlags` и `Route`; возвращает `Candidate`.

**Компоненты**

| Компонент | Что делает |
|---|---|
| **Regex detector** | Каталог правил: AWS, GitHub, Slack, JWT, private key, Bearer, database URL, generic api_key/password. Правило: `name`, `pattern`, `secret_type`; относительный вес по имени задаётся `ScoringConfig.regex_rule_weights`. Предкомпилированы, защищены от ReDoS (ограничение длины входа и квантификаторов). Сработавшие имена попадают в `Candidate.regex_rules`, флаг `REGEX`. |
| **Entropy calculator** | `H(X) = −Σ p(x)·log₂p(x)` по символам значения. Выполняется в `ThreadPoolExecutor(cpu_threads)` через `asyncio.to_thread`, чтобы не блокировать event loop. Порог зависит от алфавита (hex, base64, произвольный). Флаг `ENTROPY`. |
| **Context analyzer** | Имя переменной (`password`, `secret`, `token`, `api_key`), путь файла (`.env`, `config/`, `*.pem` повышают; `test/`, `fixtures/`, `docs/`, `example` понижают), соседние строки. Выставляет `is_test_context`, флаг `CONTEXT`. |
| **False-positive filters** | UUID, git-хеши, timestamps, placeholders (`changeme`, `xxxx`, `<your-key>`), публичные значения конфигурации. Снижают вес, но не удаляют кандидата молча. |
| **Scorer** | `score_total = min(1.0, regex + entropy + context + token_type)`, веса из `ScoringConfig`. |
| **Router** | `Route`: `≥ high` → `AI_VALIDATION`, `≥ low` → `SUSPICIOUS`, иначе `IGNORE`. |

**Свойства.** Признаки, score и route детерминированы входом и конфигурацией.
`created_at` по умолчанию ставится при создании модели; для воспроизводимого
результата вызывающий код может передать его явно. Сырое значение из
`RawCandidate` не попадает в `Candidate`.

**Локальный MVP.** `StaticAnalyzer.analyze(raw, ai_enabled=False)` по умолчанию
понижает маршрут `AI_VALIDATION` до `SUSPICIOUS`, когда внешняя проверка
отключена. При `ai_enabled=True` сохраняется трёхуровневая маршрутизация
`ScoringConfig`. Синхронный `analyze` нужен для чистых проверок;
`analyze_async` выносит расчёт в `asyncio.to_thread`. `IGNORE` возвращается
как модель, но вызывающий модуль не сохраняет её в БД. Баллы снижаются
коэффициентами из конфига для тестового контекста и ложных срабатываний.

**Пример весов по умолчанию:** regex `+0.5`, энтропия `+0.2`, имя secret/password `+0.2`, чувствительный контекст/тип токена `+0.1`.

---

## 6. `feed/` — лента задач

**Назначение.** Durable-очередь `Task` поверх `storage/`. Единый способ выдавать работу worker'ам.

**Интерфейс (протокол `Feed`)**
- `enqueue(task) -> Task` — идемпотентно по `idempotency_key`; при конфликте возвращает существующую.
- `claim(kinds, worker_id, lease_s) -> Task | None` — атомарно выбирает `QUEUED`-задачу с `not_before ≤ now` в порядке `priority, id`, переводит в `RUNNING`, ставит `lease_owner`, `lease_expires_at`.
- `heartbeat(task_id)` — продление аренды у долгих задач.
- `complete(task, result, side_effects)` / `fail(task, error)` — терминальные переходы; `complete` выполняется в одной транзакции с побочными записями (см. «Storage»).
- `reap_expired()` — задачи с истёкшей арендой возвращаются в `QUEUED` с `ErrorCode.LEASE_EXPIRED`.
- `cancel(run_id)` — переводит всё дерево запуска в `CANCELLED`; running-задачи проверяют признак между шагами.

**Как работает**
- `claim` — один атомарный `UPDATE … WHERE id = (SELECT … LIMIT 1) RETURNING *` в транзакции с немедленной блокировкой записи: два worker'а не получат одну задачу.
- Повтор: `Task.schedule_retry` → `QUEUED`, `not_before = now + base · 2^(attempts − 1)`.
- Ограничение «один активный запуск на репозиторий» обеспечивает частичный уникальный индекс.

Протокол `Feed` — граница замены: реализацию можно поменять на другой брокер, не затрагивая остальные модули.

**Реализация.** `SqliteFeed` использует `Storage.run_in_transaction(...)` и
операции `StorageSession`: выбор и смена состояния задачи происходят внутри
одного `BEGIN IMMEDIATE`. Время передаётся через UTC-clock, что позволяет
детерминированно проверять lease, heartbeat и backoff. `complete` принимает
опциональный синхронный callback для побочных записей и сохраняет его изменения
вместе со статусом задачи и `TaskResult`. `fail` применяет
`Task.schedule_retry`; для окончательного `FAILED` создаётся `TaskResult` с
`Outcome.FAILED`.

---

## 7. `orchestrator/` — запуск анализа

**Назначение.** Превращает «запустить анализ репозитория» в дерево задач и отслеживает завершение.

**Компоненты**
- **Scan planner** — обрабатывает `REPOSITORY_SCAN`:
  1. выбирает режим: нет `PROCESSED`-коммитов → `FULL`; иначе `INCREMENTAL`; `TARGETED` — из payload;
  2. `git_collector.list_commits` → `INSERT … ON CONFLICT DO NOTHING` в `commits` (статус `PENDING`) в топологическом порядке — поэтому `Commit.id` возрастает от старых коммитов к новым;
  3. выбирает коммиты запуска: `PENDING` и `FAILED`; в режиме `TARGETED` явно указанные SHA переанализируются и при статусе `PROCESSED`;
  4. режет на пачки по `batch_size`, создаёт `COMMIT_ANALYSIS` (`payload.commit_ids`), кладёт в ленту;
  5. переводит корень в `WAITING_CHILDREN`; если работы нет — сразу `SUCCEEDED` с пустым `ScanSummary`.
- Фактический режим и счётчики плана сохраняются в payload корня до перехода в `WAITING_CHILDREN`, чтобы finalizer после перезапуска собрал корректную сводку.
- Ключ идемпотентности дочерней `COMMIT_ANALYSIS` включает `root_task_id`: неуспешную пачку можно повторно поставить в новом запуске.
- **Run finalizer** — периодически и по событию завершения задачи проверяет: все ли задачи с `root_task_id = run_id` терминальны (включая `AI_VALIDATION`). Если да — агрегирует `TaskResult` в `ScanSummary`, выставляет `Outcome` (`OK`/`PARTIAL`/`FAILED`), закрывает корень, обновляет `Repository.last_scan_at`.

**Триггеры.** CLI/API (`MANUAL`), планировщик (`SCHEDULE`), webhook push (`WEBHOOK`) — все только создают `REPOSITORY_SCAN`.

**Rebase/force-push.** Новые SHA попадут в анализ как новые; исчезнувшие остаются в `commits`, история найденных секретов сохраняется.

---

## 8. `workers/` — демоны

### 8.1. Commit worker

**Назначение.** Демон, обрабатывающий `COMMIT_ANALYSIS`.

**Цикл демона**
```
loop:
    task = feed.claim([COMMIT_ANALYSIS], worker_id, lease_s)
    if task is None: sleep(poll_interval); continue
    запустить фоновый heartbeat
    выполнить пачку
    успех  → feed.complete(task, result, side_effects)   # одна транзакция
    ошибка → task.schedule_retry(error) либо fail
graceful shutdown (SIGTERM): не брать новые, дождаться текущих (таймаут), остаток вернуть в QUEUED
```

**Внутренний pipeline одной задачи** (асинхронный, очереди ограничены — backpressure):

```
commit_ids → git_collector.get_diff → diff_queue(maxsize)
          → preprocessor → raw_queue(maxsize)
          → static_analyzer (энтропия в ThreadPool) → Candidate
```

Каждый коммит обрабатывается независимо: ошибка одного → он в `commit_ids_failed`, остальные продолжаются.

**Фиксация результата — одна транзакция:**
1. `Candidate` с `route ∈ {SUSPICIOUS, AI_VALIDATION}` → upsert в `candidates` по `UNIQUE(commit_id, file_path, line_number, value_fingerprint)`;
2. для `route = AI_VALIDATION` при `ai_enabled` → `Task(AI_VALIDATION)` в ленту (идемпотентно по `candidate_id + prompt_version`);
3. прямые инциденты по `trusted_formats` — через `incident_manager`;
4. успешные коммиты → `PROCESSED` (+`processed_at`, `scan_run_id`, `last_task_id`), неуспешные → `FAILED` (+`error_code`);
5. `TaskResult(CommitAnalysisOutput)`.

Если транзакция упала — ничего не записано, задача перезапустится; дублей не будет благодаря `UNIQUE`-ограничениям.

### 8.2. AI worker

**Назначение.** Демон, обрабатывающий `AI_VALIDATION`. Цикл тот же, `kinds = [AI_VALIDATION]`; параллелизм — `Semaphore(llm_workers)` (по умолчанию 5), контроль стоимости и latency. Приоритет задач выше `COMMIT_ANALYSIS`, чтобы инциденты появлялись раньше. Логика вызова — в модуле `ai_validation/`.

**Конфиг:** concurrency, размеры очередей, `lease_s`, `poll_interval`, таймаут graceful shutdown.

**Статус реализации.** `CommitWorker` последовательно обрабатывает коммиты
задачи, продлевает аренду фоновым heartbeat и завершает текущую задачу перед
остановкой цикла по `asyncio.Event`. Все кандидаты, локальные инциденты,
статусы коммитов и `TaskResult` записываются через `Feed.complete` одной
транзакцией. Ошибка одного коммита отмечается `Commit.FAILED`; ошибка файла
пропускает файл. При отсутствии адаптера `AiTaskFactory` анализ идёт с
`ai_enabled=False`, и AI-задачи не создаются. Подключение адаптера возможно
после реализации моделей и операции `ai_validation`.

---

## 9. `ai_validation/` — модуль ИИ-валидации

**Назначение.** Обрабатывает операцию `AI_VALIDATION`, появляющуюся в ленте, когда у `Candidate` `route = AI_VALIDATION`: даёт финальный вердикт по одному кандидату.

**Пошагово**
1. Загрузить `Candidate` по `payload.candidate_id` (в БД только маскированные значения).
2. **Masking guard** — проверить, что prompt не содержит сырых значений; при подозрении → `FAILED(MASKING_VIOLATION)`, в LLM не уходит.
3. Собрать prompt. System: роль security-классификатора, определение секрета, явный список типичных false positive (UUID, hash, timestamps, тестовые данные, placeholders, публичная конфигурация, обычные случайные строки), требование строго структурированного JSON. User (динамически): repository, commit, file, line, masked code, context, token, entropy, regex matches.
4. Вызвать `LlmClient` (интерфейс; в тестах — фейк) под `Semaphore` и с таймаутом.
5. **Разобрать и провалидировать** JSON → `AiVerdict` (типы, `confidence ∈ [0,1]`, enum). Невалидный ответ → `LLM_INVALID_RESPONSE` (retry), а не «не секрет».
6. В одной транзакции: `AiVerdict` + вызов `incident_manager` + `TaskResult(AiValidationOutput)`.

**Ошибки и ретраи.** Сеть, 429, 5xx, таймаут, невалидный JSON → `retryable`; backoff 1→2→4→8 с до `max_attempts = 5`; затем `FAILED(LLM_FAILED)`: кандидат остаётся без вердикта, запуск получает `PARTIAL`, остальное не затронуто. Новый `prompt_version` даёт новый `idempotency_key`, поэтому переанализ возможен вручную.

**Приватность.** `ai.enabled = false` полностью отключает отправку: AI-задачи не создаются, инциденты возможны только по `trusted_formats`. Модель и версия prompt'а сохраняются в `AiVerdict` для аудита.

---

## 10. `incident_manager/` — инциденты

**Назначение.** Единственное место, где решается «это инцидент». Превращает `(Candidate, AiVerdict | None)` в создание или обновление `Incident`.

**Локальная реализация MVP.** `IncidentManager.process(session, candidate,
still_in_head=None)` выполняется синхронно внутри `Storage.run_in_transaction`.
Кандидат к этому моменту уже сохранён и имеет `id`; это позволяет commit worker
включить создание инцидента в общую транзакцию с остальными эффектами задачи.
Метод принимает результат проверки HEAD от вызывающего кода, не обращаясь к Git.
Повтор того же `candidate.id` не создаёт обнаружение или событие и не меняет
версию инцидента. Для нового обнаружения запись `Incident`, `Occurrence` и
`IncidentEvent` атомарна. В этой ветке вердикт не передаётся; AI-ветка и outbox
остаются отдельным этапом.

**Как работает**
1. `decide(candidate, verdict, policy)` — правила из `models.md` (§3.9): вердикт ИИ финальный (`is_secret ∧ confidence ≥ min_ai_confidence`); без вердикта — только `trusted_formats ∧ score_total ≥ min_local_score ∧ ¬is_test_context`.
2. Поиск существующего инцидента по `(repository_id, fingerprint)`.
3. Новый → создать `Incident(OPEN)`, `Occurrence`, `IncidentEvent(OPENED)`. Существующий → добавить `Occurrence`, поднять `severity`/`confidence`, при закрытом статусе — reopen (`REOPENED`).
4. `still_in_head`: через `git_collector.file_contains_in_head`; при `True` и `bump_if_in_head` — повысить severity (`SEVERITY_RAISED`).
5. Для `severity ≥ HIGH` — создать `OutboxMessage` (в той же транзакции). Отправку выполняет отдельный компонент доставки: после успешной отправки `status = SENT`, событие `NOTIFIED`.

**Что входит в рекомендацию по умолчанию.** Ротация/отзыв ключа у провайдера, затем удаление из истории (`git filter-repo`/BFG); удаление из последнего коммита ключ не отзывает.

---

## 11. `storage/` — хранилище

**Назначение.** Единственный слой с SQL: схема, миграции, транзакции. Бизнес-логики нет.

**Таблицы:** `repositories`, `commits`, `commit_parents`, `tasks`, `task_results`, `candidates`, `ai_verdicts`, `incidents`, `occurrences`, `incident_events`, `outbox_messages`. Поля, типы, ключи и индексы — в `models.md` (§3, §6).

**Требования**
- Все PK — `INTEGER PRIMARY KEY`; SHA и fingerprint — BLOB с `UNIQUE`-индексами; enum — SMALLINT; время — epoch в миллисекундах.
- Режим журнала с конкурентным чтением (WAL); для `claim` и фиксации результатов — транзакции с немедленной блокировкой записи.
- Версионирование схемы (миграции).
- Составные транзакционные операции (единица атомарности):
  - `commit_analysis_done(task, candidates, ai_tasks, commit_updates, direct_incidents, result)`;
  - `ai_validation_done(task, verdict, incident_changes, outbox, result)`;
  - `run_finalized(root_task, summary, repository_update)`.
- Сырые значения секретов в БД не хранятся; ключ HMAC живёт вне БД.

**Реализация.** Используется `sqlite3` из стандартной библиотеки. Один экземпляр
`SqliteStorage` владеет одним соединением, а операции выполняются через
`asyncio.to_thread` и сериализуются `asyncio.Lock`, поэтому синхронный драйвер не
блокирует event loop. Составные записи выполняются через `run_in_transaction(...)`
и `StorageSession`; callback целиком работает в одном worker thread внутри
`BEGIN IMMEDIATE`. Решение и ограничения зафиксированы в
[`decisions/0001-sqlite-access.md`](decisions/0001-sqlite-access.md).

Схема создаётся версионируемыми SQL-миграциями из `storage/migrations/`.
`SqliteStorage.initialize()` применяет ещё не выполненные миграции, включает
`foreign_keys`, `busy_timeout` и WAL для файловой БД. Слой предоставляет
преобразование всех уже реализованных сохраняемых моделей; таблица
`ai_verdicts` создаётся миграцией, а типизированный mapper будет добавлен вместе
с доменной моделью `AiVerdict` в треке `ai_validation`.

---

## 12. `application.py` — локальная точка запуска MVP

`App` связывает `SqliteStorage`, `SqliteFeed`, `ScanOrchestrator`,
`CommitWorker`, локальный `GitCollector`, `Preprocessor`, `StaticAnalyzer` и
`IncidentManager`. При вызове `scan(...)` регистрирует локальный репозиторий,
создаёт новый корневой запуск или возобновляет активный, планирует и выполняет
задачи до итогового `TaskResult`. Перед циклом перевыдаёт задачи с истёкшей
арендой. HMAC-ключ передаётся явно и не сохраняется; `ai_enabled=false` обязателен
в MVP. CLI `python -m application scan` читает ключ из environment и выводит
только id запуска, outcome и агрегированные счётчики. Пауза между проверками
пустой ленты задаётся `poll_interval_s` (по умолчанию 0.25 с).

## 13. `observability/` — логи и метрики

**Логи.** Структурные (JSON), с полями `task_id`, `run_id`, `commit_id`, `candidate_id`. **Никогда** — сырые значения и немаскированный `source_line`.

**Метрики**
- длина ленты по `kind`/`status`, возраст самой старой `QUEUED`-задачи, число протухших аренд;
- throughput: коммиты/с, строки/с;
- доля кандидатов по `route`, доля отправленных в LLM;
- латентность и ошибки LLM, число повторов;
- открытые инциденты по `severity`/`status`.

---

## Сквозные сценарии

**Первый запуск.** `REPOSITORY_SCAN(FULL)` → planner вставляет все SHA как `PENDING` → N×`COMMIT_ANALYSIS` → кандидаты → M×`AI_VALIDATION` → инциденты → `ScanSummary`.

**Повторный запуск.** `INCREMENTAL`: выбираются `PENDING`/`FAILED`-коммиты, `PROCESSED` пропускаются; повторная постановка той же пачки отсекается `idempotency_key`, повторный кандидат — `UNIQUE` на `candidates`.

**Высокая вероятность утечки.** Кандидат `DATABASE_PASSWORD = "…"`, `score_total = 0.8` → `AI_VALIDATION` → `AiVerdict(is_secret=true, confidence=0.96, severity=HIGH)` → `Incident(OPEN)`; секрет всё ещё в HEAD → `CRITICAL`; `OutboxMessage` → уведомление; тот же секрет в другом коммите → новый `Occurrence` в том же инциденте.

**Падение worker'а.** Аренда истекла → `reap_expired` → задача `QUEUED` → перезапуск; транзакционная фиксация исключает частичные результаты.

**LLM недоступен.** `AI_VALIDATION` уходит в retry, затем `FAILED`; запуск завершается `PARTIAL`; инциденты по `trusted_formats` открываются локально.
