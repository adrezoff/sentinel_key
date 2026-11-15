# sentinel_key

Система асинхронного поиска секретов в Git-репозиториях и их историях.
Локальные детекторы (регулярные выражения, энтропия Шеннона, контекстный
анализ) отбирают кандидатов, после чего при необходимости выполняется
дополнительная AI-валидация через LLM. Сырые значения секретов в хранилище
не попадают — только маскированные значения и HMAC-отпечатки.

Pipeline:
```
Git → diff → preprocessor (RawCandidate) → static_analyzer (regex/энтропия/контекст → score → route)
    → route: IGNORE | SUSPICIOUS | AI_VALIDATION → ai_validation (вердикт LLM) → incident_manager (Incident)
```

## Первый запуск

Требуются Python 3.11+ и установленный `git`.

1. Откройте PowerShell в корне `sentinel_key`.
2. Укажите путь к локальному Git-репозиторию вместо `C:\path\to\project`.
3. Сгенерируйте HMAC-ключ и запустите сканирование:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
python -m application scan --repository "C:\path\to\project" --database "var\sentinel_key.db"
```

Перед закрытием терминала сохраните HMAC-ключ из переменной
`SENTINEL_KEY_HMAC_KEY_B64` в менеджере секретов. При следующих запусках
подставляйте **тот же ключ**: его смена нарушит дедупликацию. База создаётся
автоматически; отдельная команда инициализации не нужна. Успешный запуск
печатает `outcome=OK` и суммарные счётчики.

## Запуск без AI (только локальные детекторы)

AI-валидация выключена по умолчанию. Запуск использует только локальные
детекторы (regex, энтропия, контекст). Подозрительные кандидаты сохраняются
как `SUSPICIOUS`, инциденты открываются для trusted форматов.

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = "<ваш HMAC-ключ>"
python -m application scan --repository "C:\path\to\project" --database "var/sentinel_key.db"
```

## Запуск с AI-валидацией (LLM)

Для включения LLM-компонента передайте флаг `--ai-enabled` и задайте
API-ключ NVIDIA Nemotron через переменную окружения:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = "<ваш HMAC-ключ>"
$env:SENTINEL_KEY_LLM_API_KEY = "<ваш ключ NVIDIA Nemotron>"
python -m application scan --repository "C:\path\to\project" --database "var/sentinel_key.db" --ai-enabled
```

При `ai_enabled=true` candidates с высоким баллом (`route = AI_VALIDATION`)
отправляются в LLM через `AiWorker` с семафором параллелизма, таймаутом и
ретряями. Перед отправкой в LLM применяется masking guard — сырые значения
секретов не покидают память процесса. В LLM уходят только маскированные
данные (`AKIA...MPLE`).

Если LLM недоступен, задача уходит в retry, затем `FAILED`; запуск
завершается с `Outcome=PARTIAL`, но локальные детекторы продолжают работать.

## Параметры CLI

```
python -m application scan --repository <путь> --database <путь к БД>
    [--mode incremental|full|targeted] [--ref main] [--batch-size 20]
    [--target-sha <SHA>] [--ai-enabled] [--poll-interval 0.25]
```

- `--mode` — режим сканирования: `incremental` (по умолчанию), `full`,
  `targeted`.
- `--ref` — Git ref для сканирования (можно повторять, по умолчанию `--all`).
- `--batch-size` — размер пачки коммитов (по умолчанию 50).
- `--target-sha` — явный список SHA для режима `targeted`.
- `--ai-enabled` — включить AI-валидацию через LLM (по умолчанию `false`).
- `--poll-interval` — пауза между проверками пустой ленты (по умолчанию 0.25 с).

Команда печатает только id запуска, `outcome` и суммарные счётчики;
значения кандидатов в вывод не попадают.
Код выхода: `0` для `OK`, `2` для `PARTIAL`, `1` для `FAILED`.

## Observability — логи и метрики

Логирование структурированное (JSON), без хранения сырых секретов. Метрики — in-memory, Prometheus-совместимые.

### Логи

Все логи выводятся через `StructuredFormatter` (JSON) с обязательными полями: `task_id`, `run_id`, `commit_id`, `candidate_id`. Сырые секреты маскируются (`AKIA***MASKED***`, `ghp_***MASKED***`, `JWT.***MASKED***`).

```python
from observability import configure_logging, get_logger

configure_logging(level=logging.INFO, json_output=True)
logger = get_logger("sentinel_key", context=LogContext(task_id=1, run_id=2))
logger.info("Candidate processed", candidate_id=5)
```

### Метрики

Доступны через `MetricsCollector` с высокоуровневыми бизнес-метриками (`BusinessMetrics`):

```python
from observability import get_metrics

m = get_metrics()
m.inc("commits_processed_total")
m.observe("llm_latency_seconds", 0.5, labels={"model": "nemotron"})
snap = m.snapshot()
```

Бизнес-метрики:
- `commits_processed_total` — счётчик обработанных коммитов
- `lines_processed_total` — счётчик обработанных строк
- `candidates_found_total{route="..."}` — кандидаты по маршруту (IGNORE/SUSPICIOUS/AI_VALIDATION)
- `llm_calls_total{model="...",success="true/false"}` — вызовы LLM
- `llm_latency_seconds{model="..."}` — гистограмма задержек LLM
- `incidents_opened_total{severity="..."}` — открытые инциденты
- `incidents_closed_total{severity="..."}` — закрытые инциденты
- `task_retries_total{kind="...",retryable="true/false"}` — ретраи задач
- `lease_expired_total{kind="..."}` — истёкшие аренды

## Как вести разработку

`master` — релиз ветка, любой merge в неё считается релизом.

Для выполнения задачи необходимо создать дочернюю ветку от `master`, закоммитить туда свои изменения и отдельным merge request-ом сделать релиз изменений.

Названия веток по шаблону `name-branch` (дефис=пробел). Коммиты крайне желательно записывать осмыслены, лучше всего — краткая фраза описывающая изменения.


## SQLite

Дополнительные пакеты не нужны: хранилище использует `sqlite3` из Python 3.11+.
Из корня репозитория создайте или обновите базу командами PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m storage init --database var/sentinel_key.db
python -m storage info --database var/sentinel_key.db
```

Первая команда создаёт каталог, включает внешние ключи и WAL для файловой базы,
затем применяет все миграции. Команды идемпотентны: `init` безопасно запускать
при каждом старте приложения. Для временной базы в тестах передайте
`--database :memory:`.

Из Python хранилище запускается асинхронно:

```python
from storage import SqliteStorage

storage = SqliteStorage("var/sentinel_key.db")
await storage.initialize()
try:
    ...
finally:
    await storage.close()
```

## Лента задач

`SqliteFeed` использует то же хранилище и реализует durable-очередь из
`docs/modules.md`: идемпотентную постановку, атомарный `claim`, lease/heartbeat,
retry с backoff, перевыдачу протухших задач и отмену дерева запуска.

```python
from feed import SqliteFeed
from models import ScanTaskPayload, Task, TaskKind, TaskResult, Outcome
from storage import SqliteStorage

storage = SqliteStorage("var/sentinel_key.db")
await storage.initialize()
feed = SqliteFeed(storage, heartbeat_lease_s=30)

queued = await feed.enqueue(
    Task.repository_scan(
        repository_id=repository_id,
        payload=ScanTaskPayload(),
        nonce=run_nonce,
    )
)
task = await feed.claim([TaskKind.REPOSITORY_SCAN], "scan-worker-1", lease_s=30)
if task is not None:
    # Для долгой операции worker периодически вызывает heartbeat(task.id).
    result = TaskResult(task_id=task.id, outcome=Outcome.OK)
    await feed.complete(task, result)
```

Перед постановкой задачи репозиторий с указанным `repository_id` должен уже
существовать. `enqueue` возвращает существующую задачу при повторном
`idempotency_key`. Побочные записи можно передать в `complete` синхронным
callback'ом `side_effects(session)` — статус задачи, результат и эти записи
будут сохранены одной транзакцией. Для `COMMIT_ANALYSIS` реализован
`CommitWorker`; пример запуска приведён ниже.

## Предобработка diff

`Preprocessor` принимает `FileDiff` из `git_collector` и выдаёт
`RawCandidate`, привязанные к `repository_id`, `commit_id`, пути файла и
номеру строки. Он распознаёт присваивания, Bearer-токены, URL с
credentials, PEM private key и длинные «голые» токены. Бинарные,
lock-, vendor- и minified-файлы отсеиваются до извлечения.

Для fingerprint нужен стабильный секретный HMAC-ключ. Сгенерируйте его
один раз, сохраните в secret manager и передавайте точке запуска через
environment. Если менять ключ между запусками, fingerprint одного и того же
секрета изменится и дедупликация не сработает.

```powershell
$env:SENTINEL_KEY_HMAC_KEY_B64 = python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Точка сборки декодирует ключ и передаёт его в модуль явно:

```python
import base64
import os

from preprocessor import Preprocessor, PreprocessorConfig

fingerprint_key = base64.b64decode(
    os.environ["SENTINEL_KEY_HMAC_KEY_B64"],
    validate=True,
)
preprocessor = Preprocessor(fingerprint_key, PreprocessorConfig())

raw_candidates = preprocessor.process(
    file_diff,
    repository_id=repository_id,
    commit_id=commit_id,
)
# Итератор RawCandidate передаётся анализатору только в памяти процесса.
```

Не сериализуйте и не логируйте `RawCandidate`: он содержит `raw_value`,
`source_line_raw` и `context_raw`. В хранилище может попасть только
`Candidate`, созданный через `RawCandidate.to_candidate(...)`.



## Статический анализ

`StaticAnalyzer` принимает каждый `RawCandidate` в памяти, проверяет формат,
энтропию, имя переменной, путь и соседние строки, затем возвращает `Candidate`
с баллами, флагами детекторов, именами правил и маршрутом. Placeholder, UUID,
hash и тестовый контекст снижают балл. Пороги, веса и ограничения длины
задаются в `ScoringConfig`.

```python
from config import ScoringConfig
from models import Route
from static_analyzer import StaticAnalyzer

analyzer = StaticAnalyzer(ScoringConfig())
for raw in raw_candidates:
    candidate = await analyzer.analyze_async(raw, ai_enabled=False)
    if candidate.route is not Route.IGNORE:
        # Передайте candidate в транзакционную запись commit worker.
        ...
```

При `ai_enabled=False` высокий балл даёт `SUSPICIOUS`. При включении AI
такой кандидат получает `AI_VALIDATION`; постановка задачи относится к
commit worker, а решение об инциденте — к `incident_manager`. Асинхронный
вызов выносит расчёты из event loop. Для локальной проверки доступен
синхронный `analyze(...)`. Анализатор не сохраняет данные и не вызывает сеть.

## Локальные инциденты

`IncidentManager` открывает инцидент только по правилам `IncidentPolicy`:
доверенный формат, достаточный `score_total` и отсутствие тестового контекста.
Для одного fingerprint в репозитории создаётся один `Incident`; каждый новый
кандидат добавляет `Occurrence`. Повтор того же кандидата не меняет граф.
Закрытый инцидент вновь открывается при новом обнаружении, а подтверждённое
наличие в HEAD повышает severity. В модуле нет вызовов Git или LLM.

Вызов выполняется в транзакции вместе с записью кандидата и результатом
задачи. `candidate` должен уже получить `id` в этой же транзакции:

```python
from incident_manager import IncidentManager

manager = IncidentManager()

def persist(session):
    saved = session.save_candidate(candidate)
    incident = manager.process(session, saved, still_in_head=None)
    # Здесь же сохраните статус коммита и TaskResult.
    return incident

incident = await storage.run_in_transaction(persist)
```

`still_in_head` принимает `True`, `False` или `None` (проверка не выполнялась).
Ветка AI-вердиктов и outbox будут подключены вместе с соответствующими
модулями.

## Воркер анализа коммитов

`CommitWorker` берёт `COMMIT_ANALYSIS` из ленты, последовательно загружает diff
по SHA, извлекает и оценивает кандидатов, применяет локальную политику
инцидентов. Heartbeat продлевает аренду, а сигнал остановки позволяет
завершить текущую задачу. До запуска должны быть сохранены `Repository`,
`Commit` и задача с `CommitAnalysisPayload(commit_ids=[...])`.

```python
import asyncio
import base64
import os

from feed import SqliteFeed
from git_collector import LocalGitCollector
from incident_manager import IncidentManager
from models import TaskKind
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import SqliteStorage
from workers import CommitWorker, WorkerSettings

async def main() -> None:
    key = base64.b64decode(os.environ["SENTINEL_KEY_HMAC_KEY_B64"], validate=True)
    async with SqliteStorage("var/sentinel_key.db") as storage:
        worker = CommitWorker(
            feed=SqliteFeed(storage),
            storage=storage,
            collector_for=LocalGitCollector,
            preprocessor=Preprocessor(key),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("commit-worker-1", (TaskKind.COMMIT_ANALYSIS,)),
        )
        await worker.run_once()  # False означает пустую ленту.
        # Для непрерывной работы: await worker.run(stop_event), где stop_event — asyncio.Event.

asyncio.run(main())
```

В MVP AI-обработка отключена: без `AiTaskFactory` высокий балл сохраняется как
`SUSPICIOUS`, даже если у корневой задачи `ai_enabled=True`. В будущем адаптер
получит только сохранённый `Candidate` с `id` и создаст версионированную задачу
`AI_VALIDATION` в той же транзакции. Подключайте его после реализации моделей
AI и регистрации этого вида задачи. CLI запускает воркер через `App`.

## Orchestrator и smoke-проверка

`ScanOrchestrator` ставит корневую `REPOSITORY_SCAN`, получает достижимые SHA
через `GitCollector`, сохраняет коммиты и их родителей, создаёт пачки
`COMMIT_ANALYSIS` и переводит корень в `WAITING_CHILDREN`. После выполнения
дочерних задач `finalize_ready()` собирает `ScanSummary` и обновляет дату
сканирования репозитория. При первом запуске `INCREMENTAL` автоматически
становится `FULL`; повторный запуск пропускает коммиты со статусом `PROCESSED`.

Для сквозной smoke-проверки нужны Python 3.11+ и установленный Git. Из корня
репозитория выполните:

```powershell
python -m tests.smoke_scan
```
Скрипт создаёт временный Git-репозиторий с сгенерированным значением, запускает
orchestrator и commit worker с временной SQLite-базой, затем повторяет scan.
Ожидаемый вывод: `SMOKE OK: commits_done=1, candidates=…, repeat_new=0`, где
`candidates` больше нуля. Временные файлы удаляются после проверки.

## Запуск тестов

Фреймворк: `pytest` (dev-зависимость). Все тесты запускаются с `PYTHONPATH=src`.

```powershell
# Установка зависимостей (dev)
pip install -e ".[dev]"

# Все unit-тесты
$env:PYTHONPATH = "src"
python -m pytest tests/unit -ra

# Integration-тесты (SQLite, AI worker)
python -m pytest tests/integration/test_ai_worker_sqlite.py -q -ra

# Smoke-тест
python -m tests.smoke_scan

# Проверка компиляции
python -m compileall -q src tests

# Real-LLM тесты (opt-in, требуют внешнюю сеть и API-ключ)
$env:SENTINEL_KEY_RUN_REAL_LLM = "1"
$env:SENTINEL_KEY_LLM_API_KEY = "<your-api-key>"
python -m pytest tests/integration/test_real_llm.py -q -ra
```

Ожидаемый вывод unit-тестов: `125 passed, 6 subtests passed`.

Для встраивания доступен `App` из `src/application.py`; он связывает те же
модули и возвращает `TaskResult` после завершения дерева задач.

> **Примечание:** Real-LLM тесты (`test_real_llm.py`) требуют валидный API-ключ NVIDIA Nemotron с доступом к модели `nvidia/nemotron-3-ultra-550b-a55b` на эндпоинте `https://integrate.api.nvidia.com/v1`. При неверных учётных данных тесты могут падать с ошибкой аутентификации.
