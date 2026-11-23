# ADR 005: transactional outbox

- Status: **Proposed**.
- Date: 2026-11-21.
- Scope: PR-A0, documentation-only.

## Context

Бизнес-запись в PostgreSQL и публикация сообщения в Redis/Celery не образуют одну транзакцию. Сбой между ними способен оставить согласованную задачу без исполнения либо повторно доставить уже обработанную команду. Прямой provider call из HTTP/AI workflow добавляет неподконтрольный внешний эффект.

## Decision

Записывать бизнес-изменение и OutboxEvent **в одной PostgreSQL-транзакции**. Это относится к разрешённым AIJobs и distribution commands. Relay получает committed события, доставляет их с lease/retry/backoff и сохраняет transport bookkeeping. Сообщение содержит event/aggregate/revision IDs и schema version, без credentials и private payloads.

Доставка — **at-least-once**. Consumer проверяет durable idempotency key, expected revision, permission, due time и ownership перед работой. После сбоя relay/consumer событие можно повторить без второй принятой AI-генерации или второй заведомо исполненной операции. ACK очереди не является бизнес-результатом.

Outbox не даёт exactly-once удалённый эффект. До сети сохраняется DeliveryAttempt; неопределённый результат переводит его в UNKNOWN и блокирует blind retry. Provider idempotency/lookup применяется только при доказанной поддержке; иначе нужна сверка. Сбой после внешнего success до DB commit также покрывается reconciliation.

## Alternatives

- Сначала commit, затем queue send без outbox: возможны потерянные задания.
- Сначала queue send, затем commit: consumer может увидеть отсутствующую/неодобренную запись.
- Распределённая транзакция с broker/provider: не поддерживается всеми участниками и не устраняет неопределённость внешнего HTTP-эффекта.
- Только периодический scan mutable таблиц: может остаться recovery механизмом, но сам по себе не фиксирует конкретную команду/revision и её причинность.

## Consequences

Нужны bounded polling/batches, leases, idempotent consumers, quarantine после лимита и метрики lag/retry. Retention не удаляет pending/UNKNOWN evidence. Схемы событий должны сохранять совместимость на окно rollout/rollback. Старый и новый writers не инициируют одно действие параллельно.

Relay, таблицы, CI и workers не добавляются в A0. Их реализация — отдельные additive PR с failure injection. Открытые вопросы: Q3, Q5, Q9, Q10 в [roadmap](../architecture-v2/roadmap.md).

## События действующего механизма публикаций

При выдаче разрешения на отправку в Telegram или VK приложение сохраняет запись в `publication_claim_events` в одной транзакции PostgreSQL с токеном публикации. Событие связывает публикацию, точную версию контента и канал с SHA-256 отпечатком токена. Новое явно разрешённое исполнение получает отдельную запись. Миграция `20261123_0031` создаёт таблицу пустой. Исторические отправки сохраняют прежний аудит. События хранятся до удаления соответствующей публикации.

Тип `legacy.social.claimed` версии 1 и поле `delivery_owner = legacy_direct` обозначают выдачу разрешения. Вызов Celery по-прежнему выполняет действующий код после фиксации транзакции. При отказе брокера запись остаётся доступной для диагностики. Читатель не вправе отправлять задание по такой записи. Фактическую доставку подтверждают результат провайдера и последующая сверка.

## Validation / links

[External actions](../architecture-v2/external-actions.md), [migration strategy](../architecture-v2/migration-strategy.md). Будущие тесты: DB rollback, broker unavailable после commit, duplicate event, relay crash, stale lease, consumer crash после provider success и восстановление без blind resend.
