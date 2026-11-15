# Модули sentinel_key

Поля, типы и ограничения моделей описаны в [models.md](models.md).
Общая схема и ограничения реализации — в [design.md](design.md).
Примеры Python — в [руководстве по встраиванию](development/embedding.md).

## Карта компонентов

| Исходники в `src/` | Ответственность |
|---|---|
| `application.py` | Сборка App, регистрация репозитория, CLI и выполнение scan до результата |
| `models/` | Доменные данные, enum, переходы статусов, payload/output и ключи идемпотентности |
| `config/` | Неизменяемые ScoringConfig и IncidentPolicy; общего загрузчика файлов нет |
| `git_collector/` | История, diff и проверка строки в HEAD; [подробный контракт](git_collector.md) |
| `preprocessor/` | Добавленные строки FileDiff → RawCandidate; извлечение и маскирование |
| `static_analyzer/` | Regex, энтропия, контекст, score и route без БД и сети |
| `feed/` | Durable-очередь Task через Storage: claim, аренда, retry, cancel |
| `orchestrator/` | Планирование пачек и финализация дерева запуска |
| `workers/` | CommitWorker, AiWorker, настройки и фабрика AI-задач |
| `ai_validation/` | Masking guard, prompt, LlmClient, FakeLlmClient и RealLlmClient |
| `incident_manager/` | Политика инцидентов, Occurrence, события и outbox |
| `storage/` | SQLite, миграции, маппинг моделей, транзакции; единственное место с SQL |
| `security/` | Общая очистка чувствительных данных |
| `observability/` | Структурные логи и метрики в памяти; [применение](operations.md) |

## Предобработка и статический анализ

Preprocessor получает FileDiff, repository_id, commit_id и явно переданный
HMAC-ключ. Пропускает бинарные, lock-, vendor-, minified-файлы и слишком
длинные строки. Извлекает присваивания, Bearer, пароли URL, PEM и длинные токены.
Вычисляет fingerprint, маскирует строку и локальный контекст. RawCandidate
содержит сырые данные только в памяти; сериализовать и логировать его нельзя.

StaticAnalyzer возвращает Candidate со ScoreBreakdown, DetectorFlags и Route.
Regex-правила покрывают AWS, GitHub, Slack, JWT, private key, Bearer, URL и
общие api_key/password. UUID, hash, placeholder и тестовый контекст снижают
оценку. Веса, пороги и ограничения находятся в ScoringConfig ([поля](models.md#4-конфигурационные-модели-не-персистятся)).
При выключенном AI маршрут AI_VALIDATION понижается до SUSPICIOUS.
IGNORE возвращается анализатором, но worker не сохраняет его в БД.
analyze_async выполняет расчёт через asyncio.to_thread; отдельной настройки
cpu_threads в текущем приложении нет.

## Feed и хранилище

SqliteFeed использует Storage.run_in_transaction и StorageSession.
Идемпотентный enqueue возвращает существующую задачу при совпадении ключа.
claim выбирает готовую QUEUED-задачу в порядке priority/id и меняет состояние
внутри BEGIN IMMEDIATE. heartbeat продлевает аренду; reap_expired перевыдаёт
задачи либо завершает их при исчерпании попыток. cancel отменяет дерево запуска.
Частичный UNIQUE-индекс допускает один активный scan на репозиторий.

complete фиксирует статус, TaskResult и синхронный callback side_effects
одной транзакцией. fail применяет retry/backoff или терминальный FAILED;
callback side_effects для fallback вызывается при окончательной ошибке.

SqliteStorage владеет одним sqlite3-соединением; asyncio.Lock сериализует
вызовы, asyncio.to_thread выносит их из event loop. Файловая БД использует WAL,
foreign_keys и busy_timeout; initialize применяет SQL-миграции.
Callback транзакции не должен выполнять сеть или Git. Решение:
[ADR SQLite](decisions/0001-sqlite-access.md).

## Orchestrator и App

ScanOrchestrator.start_scan создаёт корень REPOSITORY_SCAN или возвращает
активный запуск. Planner получает достижимые SHA и родителей, сохраняет новые
коммиты, выбирает необработанные и делит их по batch_size (по умолчанию 50).
INCREMENTAL без PROCESSED автоматически становится FULL. Текущие FULL и
INCREMENTAL пропускают PROCESSED; TARGETED переанализирует только указанные SHA.
План сохраняется в payload, корень переходит в WAITING_CHILDREN.

Ключ COMMIT_ANALYSIS включает root_task_id, поэтому новую попытку анализа
можно поставить в другом запуске. Финализатор ждёт всех потомков, включая AI,
агрегирует ScanSummary и обновляет Repository. При отсутствии работы корень
завершается сразу; ошибки детей могут дать PARTIAL, ошибка получения истории — FAILED.

App связывает компоненты и выполняет их run_once/plan_once/finalize_ready в
цикле. AI выключен по умолчанию. При включении создаёт общую LlmConfig для
клиента и DefaultAiTaskFactory; явный llm_config имеет приоритет над окружением.
HMAC-ключ передаётся явно; CLI читает его из environment.
Параметр Python App.poll_interval_s отсутствует среди флагов CLI.

## CommitWorker

Claim COMMIT_ANALYSIS → фоновый heartbeat → последовательная обработка SHA:
get_diff → Preprocessor → StaticAnalyzer. Ошибка файла пропускает файл,
ошибка коммита отмечается отдельно, остальные коммиты продолжаются.

В одной транзакции сохраняются кандидаты, задачи AI_VALIDATION, локальные
инциденты, статусы коммитов и TaskResult. Успешный коммит становится PROCESSED
после локального анализа и постановки AI-задач, независимо от будущего вердикта.
Без AiTaskFactory AI-маршрутизация выключена. При целевом пересканировании
сохранённый SUSPICIOUS может повыситься до AI_VALIDATION, если новый анализ
вычислил этот маршрут. UNIQUE-ограничения исключают дубли.

run(stop_event) прекращает брать работу после сигнала и завершает текущую задачу.
Общего менеджера демонов или обработки SIGTERM в CLI нет.

## AI worker и LLM

AiWorker получает сохранённый Candidate, проверяет маскирование и вызывает
LlmClient. RealLlmClient использует OpenAI-совместимый API через openai.AsyncOpenAI;
настройки по умолчанию и переменные окружения описаны в [эксплуатации](operations.md#окружение-llm).
FakeLlmClient используется в тестах. Семафор клиента ограничивает вызовы;
последовательный цикл App сам по себе не запускает пять AI-задач одновременно.

Клиент формирует prompt из маскированного кандидата, разбирает JSON и валидирует
типы, enum и confidence. Невалидный ответ — ошибка попытки, а не отрицательный
вердикт. Таймауты и повторы ограничены LlmConfig; задача также имеет собственный
лимит попыток (см. Task в models.md). Новый prompt_version входит в ключ AI-задачи.

Сохранение AiVerdict, изменений инцидента, outbox и TaskResult атомарно;
проверка владельца и поколения аренды отбрасывает поздний ответ старой попытки.
При окончательной ошибке AI worker применяет локальную политику trusted formats
в транзакции fail. При retry, отмене и потере аренды fallback не создаёт инцидент;
имеющийся вердикт имеет приоритет. Итог запуска при отказе AI — PARTIAL.

## Инциденты и уведомления

IncidentManager.process работает синхронно внутри транзакции вызывающего кода;
принимает сохранённый Candidate, необязательные verdict, ai_task_id и still_in_head.
Сам не вызывает Git или LLM. Положительный вердикт с достаточной уверенностью
открывает инцидент; без вердикта нужны trusted format, достаточный локальный score
и отсутствие тестового контекста. Отрицательный AI-вердикт может закрыть локальный
инцидент как FALSE_POSITIVE, если нет других подтверждающих обнаружений.

Один fingerprint в репозитории соответствует одному Incident. Новый кандидат
добавляет Occurrence, может повысить severity и переоткрыть закрытый инцидент;
повтор того же кандидата не дублирует обнаружение. Переданный still_in_head=True
может повысить severity согласно политике. App автоматически HEAD не проверяет.

Изменения создают OutboxMessage в той же транзакции. Компонент доставки и
Notifier пока отсутствуют: наличие outbox не означает отправку уведомления.
