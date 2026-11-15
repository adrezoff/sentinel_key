# Встраивание компонентов

Контракты: [модули](../modules.md), [модели](../models.md). Примеры с `...` предполагают объекты вызывающего кода.

## Лента задач

`SqliteFeed` использует то же хранилище и реализует durable-очередь из
[modules.md](../modules.md): идемпотентную постановку, атомарный `claim`, lease/heartbeat,
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
AI-ветка принимает `verdict` и `ai_task_id`; outbox сохраняется вместе с инцидентом. Доставка уведомлений не реализована.


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

Без `AiTaskFactory` высокий балл сохраняется как `SUSPICIOUS`. При включённом AI `App` передаёт `DefaultAiTaskFactory` с общей конфигурацией LLM. AI-задачи создаются в транзакции завершения commit worker.
