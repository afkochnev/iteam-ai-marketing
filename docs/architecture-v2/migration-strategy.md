# Migration strategy: additive strangler

Статус: **Target architecture / Proposed**. PR-A0 — documentation-only: никаких Alembic migrations, backfill, runtime flags, jobs, provider changes или deployment здесь нет. [ADR 002](../adr/002-modular-monolith.md), [ADR 005](../adr/005-transactional-outbox.md), [ADR 006](../adr/006-server-side-read-models.md).

## Неизменяемые условия

1. **Текущий production продолжает работать.** Новые возможности вводятся рядом с принятыми механизмами, без остановки маркетинговой работы ради переписывания системы.
2. **Новые таблицы добавляются additive** отдельными reviewed migration PR. Существующие поля/контракты сохраняются совместимыми в течение объявленного окна rollback.
3. **Старые данные не переписываются вручную.** Запрещены ручная смена исторических статусов и «исправление» старой доставки предположением. Нужные проекции/backfill выполняет проверяемый идемпотентный инструмент с mapping, provenance, dry-run, checkpoint и отчётом расхождений.
4. **Старый Publication write path отключается только после cutover.** До подтверждённого переключения он остаётся единственным владельцем записи для legacy scope. Вывод из эксплуатации не является условием запуска первой новой read projection.
5. **Legacy tables удаляются отдельным поздним release**, после независимого approval, retention/restore проверки и подтверждения отсутствия читателей/писателей. Cutover release ничего исторического не удаляет.
6. **Текущий immutable release pipeline сохраняется.** CI/tested main, immutable image digests, provenance/signatures, ручной environment approval, signed receipt и отдельное разрешение deployment остаются действующими gates. PR-A0 не меняет CI/Docker/Compose или release tooling.
7. **Big-bang rewrite запрещён.** Ни полный перенос всех каналов за один релиз, ни долгоживущая параллельная система с двумя независимыми writers не являются допустимой стратегией.

Существующий процесс описан в [immutable releases](../stage-2-release-pipeline.md) и [release operations](../release-operations.md). Упоминание этих процедур не является командой их выполнять в PR-A0.

## Последовательность и владельцы записи

| Этап | Новый элемент | Авторитетный write path | Gate к следующему этапу |
| --- | --- | --- | --- |
| A0 — baseline | Только документы и ADR | Существующий production | Review scope, терминов и открытых вопросов |
| A1 — additive foundation | Новые сущности, mapping, совместимость и тестовые contracts | Legacy для всех существующих действий; новые таблицы ещё не инициируют отправку | Migration/restore rehearsal в изолированной среде, доказательство совместимости старого release |
| A2 — shadow reads | Server-side projections и controlled backfill | Legacy; shadow ingest/projections не вызывают providers | Сверка identity, версии, времени, counts и неоднозначностей; новый UI не утверждает недоказанный success |
| A3 — ограниченный срез | Новый intent/outbox path для явно выбранного нового scope | На каждой scope boundary ровно один writer; legacy остаётся владельцем остальных | Failure/retry/reconciliation tests, policy review, согласованный canary и наблюдение |
| A4 — cutover | Постепенное переключение выбранной кампании/канала | Новый path для переключённого scope; legacy там read-only только после gate | Подтверждены routing, drain, invariants, мониторинг и план rollback |
| A5 — стабилизация | Сверка истории и расширение scope | Новый path в принятых scopes; остальные сохраняют прежнего владельца | Полное покрытие consumers, retention и доказанная восстановимость |
| A6 — позднее удаление | Отдельный cleanup release legacy tables/adapters | Новый path | Независимое review, backup/restore, отсутствие обращений, закрытое окно совместимости |

Scope cutover задаётся сервером, например по campaign/channel/action kind, и фиксируется в журнале переключения. Он не выбирается браузером или содержимым queue message. Legacy и новый path не могут отправлять одно логическое действие одновременно. Shadow copy допустима для чтения; независимая dual-write доставка запрещена.

## Mapping legacy history

| Существующий объект | Безопасная интерпретация для target |
| --- | --- |
| Campaign и стратегия | Стабильный campaign ID/mapping и точная стратегия; отсутствующий исторический snapshot отмечается неизвестным |
| ContentItem / ContentVersion / derivations | Asset и immutable AssetVersion с сохранённой provenance; исходные версии не мутируют |
| Approval | Сохранённое решение и его subject snapshot; недостающий scope не достраивается фиктивным согласием |
| Publication | Mapping на намерение и известную историю попыток; одна legacy строка не выдаётся за полный журнал неизвестных попыток |
| PUBLISHED + provider evidence | Кандидат на ExternalPlacement после проверки точного provider/target/object identity; imported evidence помечается источником |
| CANCELLED | Отмена конкретной legacy записи, а не доказательство отсутствия других размещений или отмены всего asset |
| FAILED / timeout / missing confirmation | Сохранённая ошибка/UNKNOWN, если факт доставки не доказан; backfill не запускает повтор |
| Metrics / analyses / proposals | Observations, evidence и Decision proposals с сохранёнными временами/качеством; AI-вывод не становится фактом или принятым решением |
| Task / AgentRun | Существующая история исполнения; AI Job Engine вводится через compatibility mapping, без переписывания старых runs |

Mapping должен быть уникальным и воспроизводимым по source type + source ID; логи dry-run содержат безопасные aggregate counts и расхождения. Восстановление после остановки не создаёт вторые placements, approvals или outbox commands. Чтение legacy network errors не даёт разрешения внешнего действия.

## Cutover gate

Перед отдельным разрешением переключения нужны: утверждённый scope и writer routing; проверенная exact-version семантика; полный учёт pending/claimed/UNKNOWN; остановка новых legacy claims для переключаемого scope; drain известных in-flight attempts; сверка provider facts и deduplication keys; здоровые outbox/consumers; наблюдаемость и проверенный rollback.

Остановка legacy claims — временный управляемый barrier внутри процедуры cutover. Окончательно отключить старый Publication write path для scope можно только после подтверждения переключения. Если drain/сверка не завершены, переключение не считается состоявшимся. Неизвестный исход не лечится отправкой новым writer.

Расписание, UTC instants и browser timezone представление сверяются отдельно. Повторная генерация текста, выбор последней версии и отправка просроченных historical slots не являются частью backfill/cutover.

## Rollback и совместимость

Возврат предыдущих immutable images допустим только при доказанной совместимости схемы и контрактов. Перед возвратом writer routing приостанавливает новые claims, сверяет новые confirmed/UNKNOWN действия и убеждается, что старый path не воспроизведёт уже исполненное. Новые таблицы и outbox history остаются; downgrade/drop и восстановление старого backup поверх живых бизнес-данных не являются штатным rollback.

Если старый writer не понимает уже созданные новые действия, этот scope остаётся на новом writer либо приостановлен для оператора; автоматического слепого failback нет. Конкретные recovery runbooks и thresholds должны быть приняты до A3/A4.

PR62/PR63 проходят собственный review/merge процесс. A0 не включает их commits, не меняет их base и не требует их слияния. Будущие implementation PR сверяются с актуальным main отдельно.
