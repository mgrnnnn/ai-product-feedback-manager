# Результаты локальной проверки

Дата: 2026-11-13. Окружение: macOS, системный Python 3.9.6, локальный SQLite, Node.js для проверки синтаксиса JS.

* `python3 -m unittest discover -s tests -v`: **24/24 успешно**, 21,753 сек после последних серверных изменений. Включены E2E до одной задачи и статуса, повторные версии/события, stale approvals, tenant/source ACL, ACL revoke, tombstone/TTL, split/merge/edit, неопределённость matching, masking и JSON с секретом, DLQ/retry, prompt injection без инструментов, media flags, digest limits, shadow gate, endpoint allowlist, atomic batch, watermark rollback, timeout-after-create, session/CSRF/CSP, stale lease recovery и изменение source во время вызова модели, связь существующей задачи.
* `node --check feedback_manager/static/app.js`: успешно, включая последнее добавление кнопки связи с задачей.
* `python3 -m compileall -q feedback_manager`: успешно.
* `python3 -m feedback_manager.evaluate docs/evaluation-example.jsonl`: успешно, JSON с метриками, размером выборки и Wilson-интервалами. Эти две синтетические записи не являются оценкой качества Qwen.
* Реальный Chrome: открытие localhost, вход PO, реестр (8 тем / 27 обращений), редкие пожелания (2 темы), карточка с двумя evidence и флагом unsupported PDF, явное одобрение, сохранённый статус «Одобрено» и история решений. Внешняя публикация не выполнялась. Последняя кнопка связи с существующей задачей проверена синтаксически и серверным тестом; отдельный браузерный сценарий для неё не выполнялся.

Удалённый CI ещё не запускался. Не проверены реальные корпоративные API, реальные Qwen/ASR/VL, качество на независимой истории, нагрузка и бизнес-эффект. Полный промышленный DoD не заявляется выполненным.
