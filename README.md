# sentinel_key

Система асинхронного поиска секретов в Git-репозиториях и их истории. Локальные
детекторы отбирают кандидатов, после чего при необходимости выполняется
дополнительная AI-валидация; сырые значения секретов в хранилище не попадают.

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
будут сохранены одной транзакцией. Для будущих демонов в `workers` подготовлены
`TaskHandler` и `WorkerSettings`; сам цикл и обработчики появятся вместе с
`orchestrator` и анализаторами.

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

## Как вести разработку

`master` — релиз ветка, любой merge в неё считается релизом.

Для выполнения задачи необходимо создать дочернюю ветку от `master`, закоммитить туда свои изменения и отдельным merge request-ом сделать релиз изменений.

Названия веток по шаблону `name-branch` (дефис=пробел). Коммиты крайне желательно записывать осмыслены, лучше всего — краткая фраза описывающая изменения.
