# Матрица реализации и границы приёмки

Ниже разделены реализованное программное поведение и приёмка, для которой нужны факты компании. Готовность MVP и полноценного релиза должна подтверждаться результатами проверок.

| Требование | Результат |
|---|---|
| Read-only incremental ingestion | GET-only source adapter, overlap, pagination, checkpoint, replay, version dedup; JSON import и API, без методов изменения тикетов |
| Очередь/повторы/сбой worker | SQLite job store, 3 попытки с backoff 2/4/8 сек, lease recovery 10 мин, DLQ и admin retry; payload удаляется после успеха |
| Мультимодальность | Парсеры текстовых материалов, внутренние ASR/VL adapter contracts, quality flags/fallback; реальные модели не подключены, PDF/DOCX parser не реализован |
| Чувствительные данные | Базовое regex-маскирование до сохранения тела в очередь и до extraction, повторная проверка вывода; полноценный DLP/NER и корпоративная политика требуются |
| Извлечение/валидация | Демо-правила и настраиваемый внутренний Qwen adapter; схема, enum, продукты, evidence IDs, uncertainties, versioned prompt |
| Retrieval + merge verification | Лексический retrieval + same_need verifier в scope product/type/ACL; null → отдельная тема, ручные merge/split |
| Реестр и статистика | SQLite entities, устойчивые ID, активные evidence, первые/последние даты, SQL distinct source counts, прирост двух 7-дневных окон; уникальные пользователи не считаются без законного user ID |
| Редкие старые темы | Отдельная вкладка, count ≤3 и age ≥30 дней; не зависят от порога массовости |
| PO workflow | Веб-интерфейс, review/approve/reject/edit/split/merge, история, optimistic revision checks; дополнительные систематические действия поддержки не требуются |
| Задачи и статус | Отдельный dispatch по одобренной ревизии, shadow по умолчанию, local demo tracker, internal adapter, linking через API, idempotent key, минутный status sync; ручное повторение создания при сбое |
| ACL и жизненный цикл | Product+source ACL, нет admin bypass, source version tombstone/ACL updates, expiry и очистка derived text; SSO и централизованный lifecycle пока отсутствуют |
| Наблюдаемость | Аудит read/write, correlation ID, queue counts, p95/max unprocessed age и backlog errors; доставка алертов/центральный сборщик требуют внедрения |
| Оценка | CLI evaluator с размеченным JSONL, Wilson intervals, per-product accuracy/precision/pair-recall/abstention; экспертный topic recall отдельно |
| Проверка | Автотесты unit/contract/E2E/security/recovery, CI workflow, Chrome smoke входа/реестра/rare topics/одобрения; измеренного нагрузочного теста ещё нет |
| Эксплуатация | Runbook, backup/restore инструкция, rollback, конфигурация и договоры gateway; production deployment не выполнен |

## Что нельзя подтвердить без корпоративного доступа

1. Поддерживаемые API источника и трекера, монотонные версии, tombstones/изменения ACL, гарантии идемпотентности и права на вложения.
2. Наличие указанных внутренних моделей и их точные endpoints/model names. Совместимость сервера Qwen требует отдельной проверки.
3. Качество ≥95% определения продукта/precision merge, recall тем ≥80%, SLO 95% ≤24ч. Синтетические данные не позволяют делать эти выводы.
4. Baseline за 4–6 недель, независимая историческая разметка, shadow pilot, workload PO и нулевая добавочная нагрузка поддержки в реальном процессе.
5. Политики ИБ/DLP, шифрование инфраструктуры, утверждённые сроки хранения производных данных/аудита/бэкапов, review/red-team и SSO.
6. Подписи PO, ИБ и технического владельца; ответственные on-call; реалистичный load/capacity/backup restore drill и canary/rollback в компании.
7. Экономический эффект. Инструмент не выдумывает baseline, часы или окупаемость.

## План завершения пилота

Discovery: выбрать 1–2 продукта и владельцев, зафиксировать доступы/TTL, получить обезличенную выборку и нагрузки. Затем реализовать шлюзы по contracts.md, заменить regex согласованным DLP, проверить реальные Qwen/ASR/VL и форматы вложений. Подключить HTTPS/SSO, мониторинг и backup. Прогнать экспертный holdout и нагрузку; провести shadow pilot и сравнить baseline, исправить отклонения. После подписанного go/no-go включить SHADOW_MODE=0 в canary. Расширение телеметрией и причинная оценка выручки остаются отдельной фазой.
