# ADR 009: canonical action kinds и deduplication scope

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q3; documentation-only.

## Context

Website, email и social имеют разные доказательства успеха, но должны использовать один безопасный lifecycle Intent → Revision → Attempt → Result. Без канонического action kind и business key transport retry может случайно стать новым маркетинговым действием, а provider acceptance email — ошибочно считаться доставкой получателю.

## Decision

### Canonical action kinds первого контура

- `WEBSITE_DRAFT_CREATE`;
- `WEBSITE_PUBLISH`;
- `WEBSITE_UPDATE`;
- `EMAIL_TEST_SEND`;
- `EMAIL_CAMPAIGN_SEND`;
- `SOCIAL_PUBLISH`.

Удаление страницы, unpublish, массовые SEO-изменения, social delete/edit и иные разрушительные действия не входят в A1 и требуют отдельного ADR/policy.

### Business identity

1. `DistributionIntent` представляет одно логическое внешнее действие и имеет application-generated `business_key`.
2. Уникальность business action: `(workspace_id, action_kind, delivery_target_id, business_key)`.
3. `business_key` создаётся один раз прикладной командой и не вычисляется только из title, текста, URL или времени.
4. Перенос расписания до исполнения создаёт новую IntentRevision того же Intent и сохраняет business key.
5. Замена AssetVersion, target, audience snapshot, action kind или provider payload создаёт новую revision и требует approval по ADR 010. После подтверждённого внешнего результата последующее обновление/повторная рассылка является новым action/Intent, а не revision уже выполненного эффекта.
6. Revision имеет canonical fingerprint по action kind, exact version, target, audience snapshot, schedule, payload schema/payload hash и policy version.

### Attempts and provider idempotency

1. Transport/redelivery retry не создаёт новый Intent или revision.
2. `DeliveryAttempt` уникален по `(intent_revision_id, attempt_number)` и получает execution token/lease до provider call.
3. Provider idempotency key стабилен для одного ожидаемого внешнего эффекта, если provider поддерживает этот механизм. Он не переиспользуется для другого Intent.
4. Повтор разрешён только после классифицированного безопасного `FAILED` и повторной проверки активной revision/approval. `UNKNOWN` блокирует retry до reconciliation.
5. Confirmed external identity уникальна в scope provider + exact target + object kind + external ID.

### Email cardinality

1. Один `EMAIL_CAMPAIGN_SEND` Intent связывает одну EmailVersion и один immutable AudienceSnapshot.
2. Provider campaign/batch acceptance является техническим результатом attempt, но не recipient delivery.
3. Внутренние provider batches могут иметь child-attempt records; они не создают новые маркетинговые intents.
4. Delivery, bounce, complaint, unsubscribe и click по получателям являются Observations/events. Адреса и полные recipient lists не помещаются в общий outbox, audit или runtime diagnostics.
5. `EMAIL_TEST_SEND` всегда отдельный action kind и не даёт разрешения на `EMAIL_CAMPAIGN_SEND`.

## Alternatives

- Дедупликация по payload hash: одинаковый текст в двух законных кампаниях ошибочно сливается, а небольшой metadata drift создаёт дубль.
- Новый Intent на каждый retry: теряется различие бизнес-решения и транспорта.
- Один generic `PUBLISH`: скрывает разницу draft/public, test/campaign и recipient evidence.

## Consequences

A1 должен ввести allowlisted enum, DeliveryTarget identity, business key и revision fingerprint до подключения новых adapters. Provider-specific request IDs остаются диагностикой, а не business identity.

## Validation / links

Закрывает Q3 в [roadmap](../architecture-v2/roadmap.md). Дополняет [ADR 004](004-intent-attempt-result.md), [ADR 005](005-transactional-outbox.md) и ADR 010.
