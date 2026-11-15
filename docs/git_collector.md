# Git Collector

Модуль `src/git_collector/`. Краткое описание в [`modules.md`](modules.md); здесь — контракт, бэкенды и правила расширения.

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
└── RemoteGitCollector    # HTTP(S) → локальный mirror → LocalGit
    ├── GitHubCollector  # совместимое имя адаптера
    └── GitLabCollector  # совместимое имя адаптера
```

Фабрика: `create_git_collector(location, kind=..., config=...)`.
Если `kind` не задан — HTTP(S) выбирает удалённый адаптер, путь — Local.
`SourceKind.REMOTE = 4`; прежние числовые значения сохранены.

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

### Удалённые репозитории

Поддерживаются публичные clone URL GitHub, GitLab, Bitbucket и других Git-хостингов.
URL нормализуется (схема/хост, стандартный порт, завершающий `/`); регистр пути
и суффикс `.git` сохраняются, поскольку это зависит от сервера.
SSH, credentials в URL, query, fragment и управляющие символы запрещены.
По умолчанию разрешён только HTTPS; HTTP требует явного `allow_http=True`.
Частные, loopback, link-local и остальные неглобальные IP запрещены.
Результат DNS проверяется и закрепляется в `http.curloptResolve`; редиректы и
прокси отключены. Внешние Git config, helpers, hooks, prompts, trace и `.netrc`
не наследуются. Поддержка авторизации пока остаётся через локальный клон.

`list_commits()` вызывает `ensure_mirror()`: первый раз — `clone --mirror`
во временный каталог с атомарным переименованием после успеха; затем —
`fetch --atomic --prune` всех refs. История полная, без shallow и partial clone.
Checkout и submodules не выполняются. Кэш адресуется SHA-256 нормализованного URL.
Diff и HEAD читают существующий кэш без сети, даже в долгом запуске.
HEAD сохраняет default branch первоначального клона; при её переименовании
для проверки HEAD следует передать явный `ref`.

В одном `mirror_root` действует межпроцессная блокировка ОС: одновременно
только одна операция обновления; конкурентный вызов получает `GIT_ERROR`
без сетевого запроса. Блокировка освобождается ОС и при аварийном завершении.
TTL зеркала и паузы хоста хранятся атомарно в отдельных JSON-файлах кэша
(только время, без исходного содержимого и credentials). Перед сетью сохраняется
пауза на случай аварии. Ошибка, таймаут или отмена продлевает паузу для всего
хоста; автоматических сетевых ретраев нет, в том числе внутри Git при HTTP 429.
Истёкший кэш при ошибке обновления не выдаётся за актуальный. Ошибки Git
не включают stdout/stderr сервера. Таймаут/отмена завершает дерево процессов.

Это защита от случайного шторма запросов, а не гарантия отсутствия блокировок:
лимиты провайдера и другой трафик с того же IP неизвестны. Для нескольких
процессов нужен один кэш на локальном диске; разные машины/каталоги не имеют
общего лимита. Полный клон занимает диск и содержит исходную историю:
каталог должен быть доступен только оператору. Лимит дискового пространства
задаётся средствами ОС. Запрет сохранения извлечённых сырых кандидатов
не означает запрет Git object database исходного репозитория.

Платформы используют общий механизм Git:

| Зона | Local | GitHub / GitLab |
|---|---|---|
| Auth / clone URL | путь на диске | публичный HTTP(S) clone URL |
| Обновление зеркала | не нужно | `clone` / `fetch` + mirror path |
| Список refs для FULL/INCREMENTAL | `rev-list` | то же на mirror |
| Targeted scan | SHA из payload | то же; SHA задаёт вызывающий код |
| Diff / history | `git` | **тот же** `LocalGitCollector` на mirror |

Рекомендуемый паттерн адаптера:

1. Резолв `location` → каталог зеркала (`cache_root / sha256(url).git`).
2. `ensure_mirror()` — clone/fetch (ответственность адаптера; секреты auth не логировать).
3. Все три метода протокола **делегировать** в `LocalGitCollector(mirror_path)`.

`GitHubCollector` / `GitLabCollector` наследуют общий `RemoteGitCollector`.

Не тащить в collector:

- разбор секретов, scoring, LLM;
- запись в БД / ленту задач;
- HTTP к платформе внутри `LocalGitCollector`.

## Конфигурация

`GitCollectorConfig` (frozen) живёт в модуле; значения передаются вызывающим кодом. Общего загрузчика файлов конфигурации нет; коллектор не читает application environment сам.

| Настройка | По умолчанию | Назначение |
|---|---|---|
| `max_file_bytes` | 1048576 | Максимальный размер файла |
| `git_workers` | 4 | Лимит процессов на экземпляр |
| `command_timeout_s` | 120 | Таймаут локальной Git-команды, с |
| `git_executable` | `git` | Исполняемый файл |
| `remote_cache_ttl_s` | 300 | Минимальный интервал обновления одного зеркала |
| `remote_host_interval_s` | 60 | Пауза между сетевыми операциями одного хоста |
| `remote_failure_cooldown_s` | 900 | Пауза хоста после ошибки/отмены |
| `remote_timeout_s` | 300 | Общий таймаут DNS и clone/fetch |
| `allow_http` | false | Явное разрешение незашифрованного HTTP |

Все интервалы должны быть конечными положительными числами; cooldown не
меньше интервала хоста. `mirror_root` по умолчанию `var/git-mirrors`;
CLI задаёт его через `--git-cache`. Внутри Git `http.maxRequests=1`.
Параметры транспорта соответствуют [документации Git](https://git-scm.com/docs/git-config).

## Зависимости

Только стандартная библиотека + доменный `ErrorCode`. Внешних git-библиотек нет: процесс `git` должен быть в `PATH`.

## Как добавить новый источник

1. Класс, реализующий `GitCollector`.
2. Зарегистрировать в `create_git_collector` / `SourceKind`.
3. Если источник — git-хостинг: mirror + делегирование в `LocalGitCollector`.
4. Обновить этот документ и таблицу бэкендов.
