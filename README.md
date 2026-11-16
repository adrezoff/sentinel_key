# sentinel_key

Инструмент для поиска секретов в Git-репозиториях и их истории. Основан на двух последовательных анализаторах: статическом (регулярки, вычисление энтропии Шеннона) и динамическом (интеграция с LLM). Вначале работает статический анализ, он выявляет потенциальные инциденты и если риск высокий, то следует проверка на основе LLM.

## Зависимости

- Python 3.11+, Git в `PATH`, `openai>=3.45,<4`. SQLite входит в Python.
- Для разработки: `pytest>=9.2,<10`, `pytest-asyncio>=1.4,<2`.
- Сборка: `hatchling>=1.27,<2`. Диапазоны закреплены в `pyproject.toml`.

```powershell
python -m pip install -e .
# Для разработки: python -m pip install -e ".[dev]"
```

## Запуск

Из корня проекта в PowerShell сгенерируйте HMAC-ключ один раз:

```powershell
$env:PYTHONPATH = "src"
$env:SENTINEL_KEY_HMAC_KEY_B64 = python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
python -m application scan --repository "C:\path\to\project"
```

Сохраните ключ в менеджере секретов и при следующих запусках задавайте то же
значение `SENTINEL_KEY_HMAC_KEY_B64`. Требуется base64 минимум 32 байт;
смена ключа нарушает дедупликацию. SQLite создаётся и мигрирует автоматически.

Для публичного удалённого репозитория передайте clone URL:

```powershell
python -m application scan --repository "https://github.com/OWNER/REPO.git" --git-cache "var/git-mirrors"
```

Приватные репозитории сканируются через авторизованный локальный клон.
Ограничения URL и паузы обновления — в [Git Collector](docs/git_collector.md).

Для AI задайте ключ и включите флаг:

```powershell
$env:SENTINEL_KEY_LLM_API_KEY = "<ваш API-ключ>"
python -m application scan --repository "C:\path\to\project" --ai-enabled
```

Endpoint, модель и повторы задаются через [окружение LLM](docs/operations.md#окружение-llm).
В LLM уходят маскированные данные кандидата с локальным контекстом.

| Флаг | Назначение и значение по умолчанию |
|---|---|
| `-h`, `--help` | Справка (`python -m application scan --help`) |
| `--repository` | Обязательный локальный путь или публичный clone URL |
| `--database` | SQLite, `var/sentinel_key.db` |
| `--mode` | `incremental` (по умолчанию), `full`, `targeted` |
| `--ref` | Git ref; можно повторять, по умолчанию все refs (`--all`) |
| `--target-sha` | SHA для `targeted`; повторяйте для нескольких коммитов |
| `--batch-size` | Положительный размер пачки, `50` |
| `--ai-enabled` | Включить AI; по умолчанию выключен |
| `--git-cache` | Общий каталог зеркал и лимитов запросов, `var/git-mirrors` |
| `--allow-http` | Разрешить незашифрованный HTTP clone URL |

`incremental` пропускает `PROCESSED`, при первом запуске становится `full`.
Текущий `full` также пропускает обработанные коммиты; для переанализа
используйте `targeted` с явными SHA. Команда выполняет дерево задач до результата
и печатает id запуска, outcome и счётчики. Код выхода: `0` для `OK`, `2` для
`PARTIAL`, `1` для `FAILED`. Ошибки аргументов/конфигурации также могут дать `2`.

## Авторство

Андрей Селин, Иван Крутиков. 2026
