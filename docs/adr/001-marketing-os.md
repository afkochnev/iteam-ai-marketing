# ADR 001: iTeam как Marketing OS

- Status: **Proposed** — архитектурное направление для review, не свидетельство production rollout.
- Date: 2026-11-21.
- Scope: PR-A0, documentation-only.

## Context

Управление только social posts не связывает работу маркетинга с выбором аудитории, статьями на сайте, email, SEO и бизнес-результатом. Количество созданного контента и статус отправки не объясняют, какую маркетинговую гипотезу проверяла кампания, чему она научилась и что изменить дальше. Существующие Campaign, content versions, approvals и публикации дают основу для более полного контура.

## Decision

Развивать iTeam как Marketing OS с циклом **DISCOVER → DECIDE → CREATE → DELIVER → MEASURE → LEARN**. Кампания связывает цель, competitor intelligence/SEO, маркетинговые гипотезы, стратегию, статью, illustrations and creative assets, CMS publishing, email marketing, social channels, analytics and attribution, corrective actions and experiments.

`MarketingHypothesis` является самостоятельной стабильной сущностью, а `HypothesisVersion` — неизменяемой проверяемой формулировкой: аудитория, проблема, предполагаемый механизм, ожидаемое изменение бизнес-KPI, условия проверки, допущения и ссылки на evidence. `StrategyVersion`, `ResearchVersion`, assets, experiments и decisions ссылаются на точную версию гипотезы. Кампания может последовательно проверять несколько гипотез; новая стратегия или корректирующее решение не переписывают прежнюю гипотезу задним числом.

Сохранять правило **AI proposes → app validates → human approves → app executes**. PostgreSQL остаётся source of truth. AI-вывод — предложение с evidence, а не разрешение исполнить внешнее действие. Основная мера успеха кампании — определённый человеком бизнес-KPI и результат проверки конкретной `HypothesisVersion`.

## Alternatives

- Social publishing tool: проще краткосрочно, но оставляет цель, сайт, email и learning в несвязанных системах.
- Неявная «гипотеза кампании» внутри Strategy JSON: не даёт собственной identity/version history и затрудняет связь с experiment/decision.
- Автономный AI marketing agent с прямыми внешними tools: размывает согласование, расходы и ответственность.
- Полная замена существующего приложения: создаёт ненужный риск для работающего production и отвергается.

## Consequences

Нужны доменные границы, единая provenance и контролируемый путь внешних действий. Доставка — необходимый этап, но не доказательство бизнес-эффекта. Качество источников, свежесть наблюдений, точная версия гипотезы и явные human decisions становятся частью продукта.

PR-A0 не реализует эти возможности, не меняет production и не выбирает CMS/email/analytics vendors. Принятие архитектуры не даёт разрешения на deployment или бизнес-операции. Открытые вопросы: Q1, Q2, Q8, Q12 и Q13 в [roadmap](../architecture-v2/roadmap.md).

## Validation / links

Полный [vision и Non-goals](../architecture-v2/vision.md), [domain map](../architecture-v2/domain-map.md) и [golden journey](../architecture-v2/golden-journey.md) показывают один измеримый цикл кампании. Критерий будущей приёмки — связанная цепочка hypothesis version → evidence → execution → observation → decision, а не число отправок.
