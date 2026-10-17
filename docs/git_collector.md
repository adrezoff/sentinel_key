# Git Collector

Модуль `src/git_collector/`. Краткое описание в [`modules.md`](modules.md) §3; здесь — контракт, бэкенды и правила расширения.

## Зачем

Collector — **единственная** точка доступа к истории и diff. Downstream (`preprocessor`, `orchestrator`, `incident_manager`) не вызывают `git` и не ходят в API GitHub/GitLab напрямую.

Он **не** анализирует содержимое: только SHA, родители, время коммита, потоковые `FileDiff` и проверка «строка ещё в HEAD».

## Публичный контракт

Протокол `GitCollector` (`protocol.py`):

| Метод | Назначение |
|---|---|
| `list_commits(refs) -> list[CommitInfo]` | Топологический порядок от старых к новым; SHA как `bytes` |
| `get_diff(sha) -> AsyncIterator[FileDiff]` | Пофайловый unified diff (или skip с причиной) |
| `file_contains_in_head(path, line_matcher, *, ref=None) -> bool` | Для `Incident.still_in_head` |

Репозиторий привязывается при создании коллектора (`location`), а не передаётся в каждый вызов — так проще менять бэкенд (локальный путь vs remote id).

DTO (`types.py`) — не таблицы БД:

- `CommitInfo` — `sha`, `parent_shas`, `committed_at`, `is_merge`
- `FileDiff` — путь, kind изменения, бинарность/skip, список `DiffHunk` / `DiffLine`

Ошибки модуля — `GitCollectorError` с `ErrorCode.GIT_ERROR` или `DIFF_ERROR` (см. `models.task.ErrorCode`). Сырые секреты в исключения и логи не попадают (модуль и не знает, что такое секрет).

## Бэкенды

```
GitCollector (Protocol)
├── LocalGitCollector     # git CLI на локальном клоне / bare-репозитории  ✅ реализован
├── GitHubCollector       # remote GitHub → локальный mirror → LocalGit    ⏳ заготовка
└── GitLabCollector       # remote GitLab → локальный mirror → LocalGit    ⏳ заготовка
```

Фабрика: `create_git_collector(location, kind=..., config=...)`.
Если `kind` не задан — эвристика по `location` (`github.com` → GitHub, `gitlab.` → GitLab, иначе Local).

### Local (основной путь анализа)

Реализация через `asyncio.create_subprocess_exec` (**без** `shell=True`):

1. **`list_commits`** — `git rev-list --topo-order --reverse --format=...` по переданным refs (по умолчанию `--all`).
2. **`get_diff`** — `git diff-tree -p --find-renames`:
   - обычный коммит: `sha^!` (или эквивалент parent…sha);
   - merge: только относительно **первого** родителя (`sha^1`…`sha`), чтобы влитая ветка не сканировалась повторно;
   - корневой коммит: `--root`.
3. **`file_contains_in_head`** — `git show <ref>:<path>` и проверка строк через `line_matcher`.

Ограничения из конфига коллектора:

- `command_timeout_s` — таймаут процесса;
- `max_file_bytes` — файлы больше порога → `FileDiff` с `skip_reason`, без hunks;
- `git_workers` — `asyncio.Semaphore` на параллельные git-процессы одного инстанса.

SHA перед подстановкой в argv проверяется: hex длины 40 (SHA-1) или 64 (SHA-256 object format).

### GitHub / GitLab (как подключать)

Платформы отличаются от «просто git» в зонах **вне** разбора diff:

| Зона | Local | GitHub / GitLab |
|---|---|---|
| Auth / clone URL | путь на диске | token, app install, SSH deploy key |
| Обновление зеркала | не нужно | `fetch` / API + mirror path |
| Список refs для FULL/INCREMENTAL | `rev-list` | то же на mirror; опционально API для default branch |
| Targeted scan (webhook) | SHA из payload | то же; payload приходит из API платформы |
| Diff / history | `git` | **тот же** `LocalGitCollector` на mirror |

Рекомендуемый паттерн адаптера:

1. Резолв `location` → каталог зеркала (`cache_root / owner / repo`).
2. `ensure_mirror()` — clone/fetch (ответственность адаптера; секреты auth не логировать).
3. Все три метода протокола **делегировать** в `LocalGitCollector(mirror_path)`.

Заготовки `GitHubCollector` / `GitLabCollector` сейчас поднимают `NotImplementedError` с отсылкой сюда — чтобы граница API уже была зафиксирована, а реализация auth/mirror могла прийти отдельной задачей без правок orchestrator/workers.

Не тащить в collector:

- разбор секретов, scoring, LLM;
- запись в БД / ленту задач;
- HTTP к платформе внутри `LocalGitCollector`.

## Конфигурация

`GitCollectorConfig` (frozen) живёт в модуле; значения по умолчанию согласованы с [`modules.md`](modules.md) §2. Глобальный loader конфигурации позже прокинет поля сюда — коллектор не читает файлы и env сам (кроме того, что передаст вызывающий код в адаптеры GitHub/GitLab).

## Зависимости

Только стандартная библиотека + доменный `ErrorCode`. Внешних git-библиотек нет: процесс `git` должен быть в `PATH`.

## Как добавить новый источник

1. Класс, реализующий `GitCollector`.
2. Зарегистрировать в `create_git_collector` / `SourceKind`.
3. Если источник — git-хостинг: mirror + делегирование в `LocalGitCollector`.
4. Обновить этот документ и таблицу бэкендов.
