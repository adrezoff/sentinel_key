# Разработка и проверки

## Ветки

`master` — релиз ветка, любой merge в неё считается релизом.

Для выполнения задачи необходимо создать дочернюю ветку от `master`, закоммитить туда свои изменения и отдельным merge request-ом сделать релиз изменений.

Названия веток по шаблону `name-branch` (дефис=пробел). Коммиты крайне желательно записывать осмыслены, лучше всего — краткая фраза описывающая изменения.



## Тесты

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


Для встраивания доступен `App` из `src/application.py`; он связывает те же
модули и возвращает `TaskResult` после завершения дерева задач.

Real-LLM используют настройки провайдера из [окружения LLM](../operations.md#окружение-llm) и требуют действующих учётных данных.

Полный локальный набор: `python -m pytest -ra`; real-LLM пропускаются без opt-in. Pytest добавляет `src` в путь импорта через `pyproject.toml`. Для smoke без установки задайте `PYTHONPATH=src`.

Текущий CI (`.github/workflows/unit-tests.yml`) запускает только `tests/unit` на Python 3.11 в Ubuntu. Integration и smoke запускаются отдельно. Smoke создаёт временный Git и SQLite, проверяет первый и повторный scan; успешный вывод начинается с `SMOKE OK`.
