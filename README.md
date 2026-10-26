# sentinel_key

## Первый запуск
1. Установлены Python 3.11+ и Git; PowerShell открыт в корне `sentinel_key`.
2. Вместо `C:\path\to\project` указан путь к локальному Git-репозиторию.
3. Выполнены три команды:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = py -3 -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
py -3 -m application scan --repository "C:\path\to\project" --database "var\sentinel_key.db"
```

Перед закрытием терминала сохраните HMAC-ключ из переменной
`SENTINEL_KEY_HMAC_KEY_B64` в менеджере секретов. При следующих запусках
подставляйте **тот же ключ**: его смена нарушит дедупликацию. База создаётся
автоматически; отдельная команда инициализации не нужна. Успешный запуск
печатает `outcome=OK` и суммарные счётчики.

Система асинхронного поиска секретов в Git-репозиториях и их истории. Локальные
детекторы отбирают кандидатов, после чего при необходимости выполняется
дополнительная AI-валидация; сырые значения секретов в хранилище не попадают.


## Запуск на локальном проекте

Из корня `sentinel_key` задайте путь к исходникам и **постоянный** HMAC-ключ
в environment. Ключ создаётся один раз; сохраните его в менеджере секретов и
используйте при следующих запусках с этой базой (см. раздел «Предобработка
diff»). Затем запустите:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = "<ваш ранее сохранённый base64-ключ>"
py -3.14 -m application scan --repository "C:\path\to\project" --database "var/sentinel_key.db"
```

Требуются Python 3.11+ и установленный `git`. Для другого интерпретатора
замените `py -3.14` на `python`. По умолчанию сканируются все refs (`--all`):
первый запуск проходит историю, следующие обрабатывают новые и ранее
неуспешные SHA. База и результаты сохраняются по указанному пути; повторный
вызов возобновляет незавершённый запуск. В MVP работает только локальный Git,
AI выключен.

Варианты: `--ref main` для выбранного ref, `--batch-size 20` для размера пачки,
`--mode targeted --target-sha <полный SHA>` для повторного анализа конкретного
коммита. Команда печатает только id запуска, `outcome` и суммарные счётчики;
значения кандидатов в вывод не попадают. Код выхода: `0` для `OK`, `2` для
`PARTIAL` или ошибки параметров, `1` для `FAILED` или ошибки запуска.

Из Python:

```python
from application import App
from models import ScanTaskPayload

async with App("var/sentinel_key.db", fingerprint_key) as app:
    result = await app.scan("C:/path/to/project", ScanTaskPayload(ai_enabled=False))
```

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
py -3.14 -m tests.smoke_scan
```

Если установлен другой Python 3.11+, используйте `python -m tests.smoke_scan`.
Скрипт создаёт временный Git-репозиторий с сгенерированным значением, запускает
orchestrator и commit worker с временной SQLite-базой, затем повторяет scan.
Ожидаемый вывод: `SMOKE OK: commits_done=1, candidates=…, repeat_new=0`, где
`candidates` больше нуля. Временные файлы удаляются после проверки.

Для встраивания доступен `App` из `src/application.py`; он связывает те же
модули и возвращает `TaskResult` после завершения дерева задач.
