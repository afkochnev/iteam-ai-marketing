# ADR 004: intent, attempt и result

- Status: **Proposed**.
- Date: 2026-11-21.
- Scope: PR-A0, documentation-only.

## Context

Одно поле статуса не может одновременно описывать план, сетевую попытку, факт внешнего размещения и наблюдение бизнес-результата. Отмена старой записи не отменяет уже опубликованный пост. Потеря ответа provider не доказывает отсутствие отправки, а наличие worker не доказывает доставку.

## Decision

Использовать три отдельных понятия: **DistributionIntent** — разрешённое намерение с immutable revisions; **DeliveryAttempt** — конкретный сетевой запуск; **ExternalPlacement** — подтверждённая внешняя идентичность/результат с историей версий. Дополнительно Observation фиксирует факт измерения, Decision — предложение или принятое человеком решение на основании evidence.

Согласование content отделено от execution approval. Attempt связывает exact version/target и execution ownership token. UNKNOWN блокирует blind retry до supported reconciliation; cancel intent останавливает будущие claims, но не стирает известные факты и не отзывает уже отправленный запрос.

Успех соответствует конкретному action kind: CMS draft не равен публичной статье; email acceptance не равен доставке адресату. Позднее валидное подтверждение доставки сохраняется и сверяется, даже если intent уже изменён/отменён. Автоматической гарантии exactly-once внешнего эффекта нет.

## Alternatives

- Единый Publication status для плана/попыток/результата: неоднозначная история и опасные повторные действия.
- Только provider logs как факт: недостаточная связность с approval, версией и Campaign.
- Считать любой timeout неотправленным: создаёт дубли при фактическом success без ответа.

## Consequences

UI должен показывать отдельно расписание, попытки и подтверждённые placements. Нужны evidence-based reconciliation и понятные действия оператора. Разные каналы имеют общий контракт, но сохраняют разные уровни подтверждения и cardinality.

Текущий Publication не заменяется этим PR. Миграция и mapping historical evidence выполняются отдельно без выдумывания отсутствующих фактов. Открытые вопросы: Q3, Q4, Q6, Q10 в [roadmap](../architecture-v2/roadmap.md).

## Validation / links

[Entity model](../architecture-v2/entity-model.md), [external actions](../architecture-v2/external-actions.md), [migration strategy](../architecture-v2/migration-strategy.md). Проверки: cancelled old record + published placement, wrong target, duplicate event, response loss и late confirmation.
