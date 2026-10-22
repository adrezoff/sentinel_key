Я бы двигался так:

1. **Preprocessor**

   Следующий лучший шаг. Он превращает Git diff в `RawCandidate` и задаёт главный security-инвариант: сырой секрет существует только в памяти.

   Для MVP достаточно поддержать:

   - присваивания `KEY=value`, `key: value`, `export KEY=value`;
   - Bearer-токены;
   - URL с credentials;
   - «голые» длинные токены;
   - расчёт HMAC fingerprint;
   - маскирование значения, строки и контекста;
   - дедупликацию внутри одного diff.

2. **Static analyzer**

   После preprocessor можно реализовать полностью автономный локальный pipeline:

   - небольшой каталог надёжных regex;
   - энтропию;
   - анализ имени переменной и пути файла;
   - фильтры UUID, hash, placeholder и тестового контекста;
   - расчёт `ScoreBreakdown`;
   - выбор `IGNORE` или `SUSPICIOUS`.

   Для MVP я бы временно не создавал маршрут `AI_VALIDATION`, когда AI отключён: кандидат с высоким score сохраняется как `SUSPICIOUS`, а инцидент создаётся только для `trusted_formats` согласно уже описанной политике.

3. **Incident manager — только локальная ветка**

   Не обязательно реализовывать AI-часть. Достаточно:

   - `IncidentPolicy.should_open(candidate, verdict=None)`;
   - дедупликации по `(repository_id, fingerprint)`;
   - создания `Occurrence`;
   - reopen закрытого инцидента;
   - повышения severity;
   - записи `IncidentEvent`.

   Outbox и реальные уведомления можно оставить на потом.

4. **Commit worker**

   К этому моменту уже будет готов полезный pipeline:

   `COMMIT_ANALYSIS → Git diff → preprocessor → static analyzer → candidates → local incidents`

   Первый worker стоит сделать максимально простым:

   - последовательная обработка коммитов;
   - ограниченные очереди или даже последовательный pipeline;
   - heartbeat;
   - независимая фиксация успеха/ошибки каждого коммита;
   - единая итоговая транзакция через существующие storage/feed.

   Параллелизм и тонкую настройку backpressure лучше добавить после работающего end-to-end сценария.

5. **Orchestrator**

   После рабочего commit worker:

   - создать корневую `REPOSITORY_SCAN`;
   - получить список SHA;
   - сохранить новые коммиты;
   - разбить их на `COMMIT_ANALYSIS`;
   - дождаться терминальных дочерних задач;
   - сформировать `ScanSummary`.

6. **Минимальная точка запуска**

   Последним шагом MVP — простой CLI: путь к репозиторию и команда запуска сканирования. Без API, планировщика и daemon-management.
