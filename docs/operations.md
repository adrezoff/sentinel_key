# Эксплуатация

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


## Логи и метрики

Логирование структурированное (JSON), без хранения сырых секретов. Метрики хранятся в памяти; встроенного Prometheus endpoint/exporter нет.

### Логи

Все логи выводятся через `StructuredFormatter` (JSON) с обязательными полями: `task_id`, `run_id`, `commit_id`, `candidate_id`. Сырые секреты маскируются (`AKIA***MASKED***`, `ghp_***MASKED***`, `JWT.***MASKED***`).

```python
import logging
from observability import configure_logging, get_logger
from observability.logger import LogContext

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

`BusinessMetrics` предоставляет методы регистрации метрик; это не гарантия автоматического заполнения всех счётчиков pipeline.

## Окружение LLM

При включении AI `App` использует `LlmConfig.from_env()`, если конфигурация не передана явно. `.env` автоматически не загружается.

| Переменная | По умолчанию                       |
|---|------------------------------------|
| `SENTINEL_KEY_LLM_API_KEY` |                                    |
| `SENTINEL_KEY_LLM_ENDPOINT` | `https://integrate.api.nvidia.com/v1` |
| `SENTINEL_KEY_LLM_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` |
| `SENTINEL_KEY_LLM_TEMPERATURE` | `0.1`                              |
| `SENTINEL_KEY_LLM_MAX_TOKENS` | `4000`                             |
| `SENTINEL_KEY_LLM_TIMEOUT` | `30.0 с`                           |
| `SENTINEL_KEY_LLM_MAX_ATTEMPTS` | `3`                                |
| `SENTINEL_KEY_LLM_RETRY_BASE` | `1.0 с`                            |
| `SENTINEL_KEY_LLM_MAX_CONCURRENT` | `5`                                |
| `SENTINEL_KEY_LLM_PROMPT_VERSION` | `2`                                |

Это значения реализации, а не каталог моделей провайдера. `SENTINEL_KEY_USE_FAKE_LLM=1` (также `true`, `yes`) выбирает тестовый клиент при включённом AI. HMAC-ключ и scan описаны в [README](../README.md), зеркала — в [Git Collector](git_collector.md).
