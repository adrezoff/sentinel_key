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



## Как вести разработку

`master` — релиз ветка, любой merge в неё считается релизом.

Для выполнения задачи необходимо создать дочернюю ветку от `master`, закоммитить туда свои изменения и отдельным merge request-ом сделать релиз изменений.

Названия веток по шаблону `name-branch` (дефис=пробел). Коммиты крайне желательно записывать осмыслены, лучше всего — краткая фраза описывающая изменения.
