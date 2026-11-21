# ADR 010: approval scopes, изменение revision и делегирование

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q4; documentation-only.

## Context

Согласование текста и разрешение внешнего эффекта — разные решения. Одобрение статьи не должно автоматически разрешать отправку любому target, аудитории и времени. Одновременно безопасный retry той же попытки не должен требовать нового маркетингового решения при каждом transport failure.

## Decision

1. Различаются минимум два класса:
   - `SUBJECT_APPROVAL` — exact HypothesisVersion, ResearchVersion, StrategyVersion или AssetVersion;
   - `EXECUTION_APPROVAL` — exact IntentRevision внешнего действия.
2. Approval immutable и хранит workspace/campaign, subject type/id/version/hash, scope, policy version, reviewer, decision, comment, timestamps, optional expiry и audit correlation.
3. Execution approval связывает exact:
   - action kind;
   - AssetVersion;
   - DeliveryTarget;
   - AudienceSnapshot, если применимо;
   - schedule instant либо явно утверждённое schedule window;
   - payload hash/schema version;
   - IntentRevision ID.
4. Любое изменение AssetVersion/creative, action kind, target, audience snapshot или payload hash всегда требует новой execution approval. Content approval новой версии не переносится автоматически на старый Intent и наоборот.
5. Schedule-only change создаёт новую IntentRevision. По умолчанию требуется новая execution approval. Повторное использование возможно только если прежнее approval явно содержит schedule window/timezone и новое время входит в него.
6. Safe retry той же exact revision после классифицированного `FAILED` может использовать то же execution approval в пределах expiry/retry policy. `UNKNOWN`, revoke, expiry, новая active revision или изменённая policy блокируют попытку.
7. Approval повторно проверяется непосредственно перед claim и перед provider call. Отмена/ревокация после начала attempt не стирает историю и не доказывает отсутствие внешнего эффекта.

## Делегирование и роли

- AI, worker и service identities не могут принимать approval decisions.
- `MARKETING_MANAGER` может согласовывать subject и execution только внутри своего workspace и только для заранее настроенных DeliveryTarget.
- `WORKSPACE_ADMIN` дополнительно управляет policy, membership, provider targets и credential references.
- Назначение Task, владение Campaign или наличие provider credential не делегирует approval право.
- Делегирование выполняется только capability membership/policy и всегда аудируется.
- A1 не навязывает обязательный four-eyes для всех небольших команд: policy может требовать `reviewer != requester`. Независимо от этого human reviewer всегда отличается от AI/service actor.
- Изменение credential, provider target identity или approval policy требует `WORKSPACE_ADMIN`; worker не может расширить scope из сообщения очереди.

## Consumption semantics

Execution approval разрешает один логический внешний эффект, а не неограниченное число provider calls. Число попыток ограничено retry policy; confirmed success закрывает разрешение. Новый внешний эффект требует нового Intent/approval.

## Alternatives

- Одно approval на AssetVersion: не связывает target/audience/time и позволяет scope expansion.
- Новое approval на каждый network retry: смешивает бизнес-решение с transport mechanics.
- Неявное делегирование через роль исполнителя: credential possession превращается в право принимать решение.

## Consequences

A1 потребует typed approval subject/scope, policy version и серверный расчёт `available_actions`. Legacy Approval остаётся compatibility source до cutover; vNext adapter не должен домысливать отсутствующий execution scope.

## Validation / links

Закрывает Q4 в [roadmap](../architecture-v2/roadmap.md). Связано с ADR 007, ADR 009 и [external actions](../architecture-v2/external-actions.md).
