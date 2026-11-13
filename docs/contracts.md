# Контракты v1

Все внешние для приложения сервисы находятся внутри корпоративного контура. Конкретные корпоративные API неизвестны: здесь описаны адаптерные шлюзы, а не утверждение об уже существующих API компании. HTTP ошибки/невалидные ответы вызывают retry или ручную проверку. Таймаут — 45 секунд, максимальный ответ — 4 МБ. Токены передаются в Authorization; redirect запрещён, TLS проверяется системным trust store.

## Источник — только чтение

`GET /records?updated_since=<ISO>&updated_until=<ISO>&cursor=<opaque>&limit=500`

Ответ: `{"records":[...],"next_cursor":null}`. Формат записи см. `source-example.json`. Версия — **монотонно растущее целое**, дата — ISO 8601 с timezone. Непрозрачные upstream-версии должен нормализовать шлюз. Обновление текста, ACL, удаления или срока хранения обязано повышать версию. Удаление передаётся тем же envelope с `deleted:true`. Пропавшую из выгрузки запись нельзя считать удалённой.

Обязательны source_id, source_version, product_id, created_at, updated_at, permitted_acl. ACL — явный список известных ID пользователей с доступом к продукту. Пустой/неизвестный ACL отклоняет весь batch. Не передавайте ПДн в ID и метаданных. `expires_at` задаётся политикой, по умолчанию created_at + 365 дней (это локальное допущение, не утверждённый корпоративный TTL).

Checkpoint сохраняется только после всех страниц. Overlap 10 минут; дедупликация по source/version/pipeline. Возобновление после частичного сбоя безопасно. Gateway обязан сохранять стабильную пагинацию в окне и передавать ACL/tombstones независимо от наличия текста. Большие вложения — защищённые ссылки, а не base64 в JSON. Версии после успешной обработки не завышают статистику.

## Qwen

`POST <QWEN_URL>/chat/completions` с `model`, `temperature:0`, `response_format:{"type":"json_object"}`, messages. Ответ: `choices[0].message.content` — JSON строка. Контракт extraction и системный промпт версии 1.0 находятся в `feedback_manager/models.py`. Проверяются enum, типы, разрешённый продукт и точный evidence_id. Ответ модели повторно маскируется. Неверный JSON не попадает в реестр; после трёх попыток — DLQ.

Matching возвращает `{"same_need":true|false|null}`. Retrieval ограничен тем же продуктом, ACL и типом, ранжируется лексическим пересечением; максимум 5 кандидатов проверяются моделью. Это базовый retrieval для малого пилота; для измеренных больших объёмов заменить внутренним полнотекстовым индексом. `true` связывает evidence, `false/null` создаёт отдельную тему на проверку. Embeddings не требуются. Демо объединяет только точное нормализованное совпадение потребности.

## ASR / VL

`POST <MEDIA_URL>/transcribe` либо `/describe`:

```json
{"ref":"opaque-internal-file-ref","source_id":"TICKET-1","source_version":1}
```

Ответ: `{"text":"маскированный текст","quality":"accepted"}`. `kind` вложения — `audio` или `image`. Gateway должен проверить права source service на ref, запретить произвольные URL, применить согласованные DLP/redaction правила до LLM. Текст дополнительно маскируется приложением. Низкое качество и недоступность помечаются, основной текст продолжает обработку. Без текста после preprocessing задание идёт в retry/DLQ.

Встроены парсеры `text/plain`, `text/markdown`, `text/csv` через поле `text`. PDF/DOCX и бинарные форматы сейчас помечаются unsupported; их внедрение — отдельный parser adapter после согласования безопасного стека. Демо не имитирует распознавание аудио/изображений.

## Трекер

`POST <TRACKER_URL>/issues`:

```json
{"topic_id":"...","product_id":"portal","approved_revision":3,"approved_by":"po","idempotency_key":"topic:<id>","title":"...","description":"..."}
```

Ответ: `{"id":"DEV-123"}`. **Gateway обязан гарантировать устойчивую идемпотентность ключа**, в том числе timeout-after-create. Без этой гарантии нельзя включать промышленное создание задач. Ключ привязан к теме, не к версии. До dispatch транзакция проверяет актуальное одобрение и ACL; изменение источников блокируется на время dispatch. Для малого пилота допустимо, при масштабировании заменить полноценным outbox с fencing/version check и reconciliation. Автоматического принятия продуктовых решений нет.

`GET /issues/<URL-encoded-id>` → `{"id":"DEV-123","product_id":"portal","status":"open"}`. Статусы: open, in_progress, done, cancelled. Gateway проверяет права на продукт и задачу; forbidden/неизвестные статусы фиксируются как sync error. Sync — каждую минуту.

Для связи с существующей задачей передайте `external_issue_id` в одобренный вызов `/api/topics/<id>/backlog`. Приложение прочитает задачу и проверит совпадение product_id. Исходные тикеты не обновляются. В демонстрационном режиме существует SQLite-трекер с ID `DEMO-*`, не реальная система разработки.

## HTTP API приложения

GET `/health` — liveness без данных. Остальные API требуют сессию, кроме POST `/api/login` с username/password. Пароли PBKDF2-HMAC-SHA256, 600000 итераций, индивидуальная соль. Cookie HttpOnly, SameSite=Strict, срок 8 часов; в HTTPS-контуре включить Secure. `GET /api/me` возвращает CSRF и роль. Все POST с сессией требуют заголовок `X-CSRF-Token` и JSON Content-Type.

| Метод и путь | Роль | Назначение |
|---|---|---|
| GET /api/topics?q=&type=&status=&rare=1 | PO, admin с явным ACL | Реестр с ограничением прав |
| GET /api/topics/:id | Авторизованный участник ACL | Карточка, активные evidence, история |
| POST /api/topics/:id/review | PO | action, revision, reason; edit: title/user_need; merge: target_id; split: title/evidence_ids |
| POST /api/topics/:id/backlog | PO | approved_revision, approved_by, idempotency_key; опционально external_issue_id |
| POST /api/digest | PO, admin с явным ACL | До 10 тем, подавление повторов 7 дней; создаёт запись выпуска, не отправляет сообщения |
| POST /api/ingest | source | records: массив ≤500 |
| GET /api/operations | admin | Очередь, метрики возраста, ошибки sync, аудит без текстов |
| POST /api/jobs/retry | admin | job_id из DLQ |
| POST /api/logout | Любая сессия | Завершение сессии |

Ошибки: 400 — контракт, 401 — нет сессии, 403 — роль/CSRF, 404 — нет доступа/объекта, 409 — устаревшая версия/нет одобрения/shadow mode, 413 — размер, 429 — login rate limit. Ошибка dispatch сохраняется в backlog.sync_status=error (ответ 200 содержит состояние операции); повторный одобренный вызов безопасен. SQL параметризован, UI экранирует текст, CSP запрещает сторонние скрипты. Журналы хранят correlation ID и классы ошибок без сырых ответов upstream.
