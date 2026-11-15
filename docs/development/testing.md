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

CI (`.github/workflows/unit-tests.yml`) запускает весь набор с `-m "not real_llm"`, smoke и compileall на Python 3.11 в Ubuntu и Windows. Таймаут job — 10 минут. Smoke создаёт временный Git и SQLite, проверяет первый и повторный scan; успешный вывод начинается с `SMOKE OK`.


## Сквозное покрытие

| Сценарий | Проверка |
|---|---|
| Первый scan и повтор без дублей | `test_scan_lifecycle.py::test_three_commits_deduplicate_repeat_and_reopen`, `tests/smoke_scan.py` |
| Merge и force-push с изменённым графом SHA | `test_remote_scan.py::test_remote_app_full_repeat_fetch_and_force_push` |
| Потеря worker посреди пачки, истечение аренды и повторная обработка | `test_scan_lifecycle.py::test_resume_after_commit_worker_loses_lease` |
| Отказ LLM → PARTIAL и локальный fallback | `test_ai_validation_regressions.py::test_trusted_format_fallback_when_llm_fails` |
| Один секрет в трёх коммитах → один Incident и три Occurrence; reopen | `test_scan_lifecycle.py::test_three_commits_deduplicate_repeat_and_reopen` |
| Старый ответ после перевыдачи аренды не фиксируется | `test_ai_validation_review.py::test_same_worker_stale_attempt_must_not_commit_after_reclaim` |
| Откат AI-вердикта, инцидента и результата | `test_ai_worker_sqlite.py::test_rollback_removes_verdict_when_incident_changes_fail` |
| Закрытие HTTP и SQLite, включая ошибки | `tests/unit/test_resource_cleanup.py` |

Пути `test_*.py` в таблице относятся к `tests/integration/`, если не указано иначе.
Потеря worker моделируется отменой обработки после первого коммита, а время аренды
передаётся явно; тест не зависит от длительных sleep или реального убийства процесса.

Тестовый Git изолирован от пользовательских подписей, hooks и конфигурации.
Bare-репозиторий HTTP-фикстуры создаётся через init/push: локальный clone
зависал на Windows. Git-процессы этой фикстуры ограничены 20 секундами.
`faulthandler_timeout=60` печатает стеки зависшего pytest; это диагностика,
а не принудительный таймаут отдельного теста.
