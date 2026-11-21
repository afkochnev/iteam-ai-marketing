# ADR 003: versioned assets и immutable evidence

- Status: **Proposed**.
- Date: 2026-11-21.
- Scope: PR-A0, documentation-only.

## Context

Один Campaign содержит статью, иллюстрацию, email и канальные адаптации. «Текущая версия» может измениться между согласованием и отправкой. Сходство названия не доказывает идентичность материалов. Без точных snapshots нельзя объяснить, какой текст/creative был утверждён, опубликован или использован при анализе.

## Decision

Разделить стабильную идентичность Asset и immutable AssetVersion. Версионировать также исследования и стратегии. AssetVersion включает содержимое, title/metadata, provenance, media object version/hash и связи к точным исходным версиям. Канальная адаптация — самостоятельная версия/asset, а не динамическая выборка последней статьи при отправке.

Approval фиксирует exact subject snapshot/hash. IntentRevision связывает одобренную версию с target/action/schedule; DeliveryAttempt использует эту revision. Новая версия не заменяет старую автоматически. Перенос времени сохраняет выбранную редакцию, пока человек отдельно не разрешит её замену.

PostgreSQL хранит авторитетные metadata и ссылки; binary storage сохраняет immutable bytes/version/hash. Наблюдения и evidence не исправляются in-place: новая запись может явно supersede старую.

## Alternatives

- Mutable content row и выбор latest: проще хранение, но согласование и внешний результат могут относиться к разному тексту.
- Полная копия без provenance для каждого канала: сохраняет payload, но теряет объяснимость производных материалов.
- Только название/URL как identity: приводит к ложному объединению редакций и публикаций.

## Consequences

Потребуются retention, typed provenance и версии creative metadata. Текущий указатель для удобства UI допустим, но не является субъектом согласования/доставки. Обновление существующей CMS-страницы создаёт новую связь PlacementRevision с версией, сохраняя прежнюю историю.

Физическая схема и binary storage не выбираются в A0. Открытые вопросы: Q2, Q4, Q7 в [roadmap](../architecture-v2/roadmap.md).

## Validation / links

[Entity model](../architecture-v2/entity-model.md), [external actions](../architecture-v2/external-actions.md). Будущие тесты должны покрывать v1 scheduled + v2 approved, перенос без замены текста, exact creative reference, provenance и отсутствие склейки по title.
