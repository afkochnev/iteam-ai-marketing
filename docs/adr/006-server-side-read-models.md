# ADR 006: server-side read models

- Status: **Proposed**.
- Date: 2026-11-21.
- Scope: PR-A0, documentation-only.

## Context

Если браузер собирает Campaign, versions, approvals, publication lists и метрики независимыми запросами, он может смешать разные моменты времени и показать отменённую историю как действующее состояние. Разные страницы начинают по-разному вычислять «утверждено», «запланировано» и «опубликовано».

## Decision

Формировать read models на сервере из PostgreSQL: campaign workspace, asset/version review, delivery timeline, measurement/evidence и decision view. Они явно разделяют intent, attempts, confirmed placements и historical cancellations, сохраняют exact version identity, target и ссылки на источник факта.

DTO содержит `as_of`, completeness/freshness, unknown/missing/stale и разрешённые текущему пользователю действия. Возможен транзакционный query либо асинхронная projection; для последней необходим watermark/lag и возможность rebuild. На начальном этапе предпочтителен минимально достаточный серверный query; отдельная CQRS-платформа не требуется.

Read model не является новым source of truth и не пишет бизнес-состояние. Любая command повторно проверяет authorization, approval, expected revision и актуальное состояние в БД: наличие кнопки в ранее полученном DTO не даёт права на действие. UI отвечает за ввод, навигацию и локальное представление времени, а не доменное восстановление статусов.

Обычный GET не выполняет provider calls, credential probes, AI jobs или скрытые business mutations. Измерения поступают отдельным управляемым ingestion workflow. Недоступность источника отображается как unknown/stale, а не ноль или «доставлено».

## Alternatives

- Client-side joins и business rules в каждой странице: противоречивые статусы и повторение логики.
- Вызов всех providers при рендере: утечки полномочий, нестабильная задержка и неуправляемые внешние обращения.
- Отдельная event-store/CQRS система сразу: преждевременная сложность; PostgreSQL projections достаточны для baseline.

## Consequences

Нужны bounded queries/pagination, единый контракт статусов, RBAC и тесты частичной/устаревшей информации. Derived totals воспроизводимы, обновления не переписывают исторические факты. Переход UI/read paths возможен до cutover writers через shadow parity.

A0 не меняет endpoints или frontend. Открытые вопросы: Q1, Q8, Q9 в [roadmap](../architecture-v2/roadmap.md).

## Validation / links

[Domain map](../architecture-v2/domain-map.md), [entity model](../architecture-v2/entity-model.md), [migration strategy](../architecture-v2/migration-strategy.md). Будущие проверки: published + cancelled history, snapshot consistency, stale projection, RBAC, wrong version и action rejection после смены revision.
