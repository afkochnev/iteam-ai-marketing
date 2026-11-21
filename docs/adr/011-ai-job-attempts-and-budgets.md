# ADR 011: AIJob retry, cancellation и budget semantics

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q5; documentation-only.

## Context

Legacy `Task` и `AgentRun` одновременно представляют пользовательскую работу, orchestration и конкретное обращение к модели. Target architecture должна отделить долговечное разрешённое вычисление от попыток provider, учитывать стоимость и не позволять позднему ответу отменённой задачи продвинуть бизнес-workflow.

## Decision

1. `AIJob` — одно логическое вычисление с immutable input snapshot, requested output schema, exact domain references, prompt/model/tool policy snapshot, initiator, workspace/campaign scope, generation key, deadline и budget.
2. `AIJobAttempt` — конкретная серия model requests в рамках одного AIJob. Retry создаёт новую attempt, но не новый AIJob, пока input/policy/output contract не изменились.
3. Изменение входной версии, prompt policy, output schema, разрешённых tools или business purpose создаёт новый AIJob с новым generation key.
4. Принятый результат уникален на logical generation key. Duplicate/later attempt может сохраняться как техническая история, но не создаёт вторую AssetVersion/HypothesisVersion.

### Budget snapshot

AIJob фиксирует до queueing:

- `max_attempts`;
- `max_provider_requests`;
- optional max input/output/total tokens;
- optional max estimated cost в заданной валюте/model price version;
- deadline;
- tool-call limits;
- authorization actor/policy version.

Перед каждым новым provider request worker атомарно проверяет остаток. Фактический ответ может превысить оценку последнего запроса; расход записывается полностью, но следующий request не запускается. Unknown cost/token usage не считается нулём.

### Retry

- Retry разрешён для allowlisted transient/validation failures и только при наличии budget/deadline.
- Invalid business input, revoked authorization, policy mismatch и исчерпанный budget являются terminal/blocking результатом.
- Retry не расширяет tool permissions и не меняет input snapshot.
- Queue redelivery использует ownership lease/idempotent claim и не создаёт новую attempt без server-side решения.

### Cancellation and late results

- Cancel фиксирует `cancellation_requested_at` и запрещает новые requests.
- Running attempt проверяет cancellation до и после provider call.
- Поздний provider result может быть сохранён для cost/audit, но помечается non-promotable и не меняет domain state.
- Cancellation не скрывает уже понесённую стоимость.

### Tool boundary

AIJob может использовать только versioned allowlist. External publishing, email send, CMS write, credential management и approval decisions запрещены AI tools. AI возвращает proposal; прикладной сервис отдельно валидирует и атомарно создаёт domain version/outbox при наличии human approval.

## States

Физический enum определяется implementation PR, но должен различать как минимум job lifecycle (`QUEUED`, `RUNNING`, `WAITING_REVIEW`, `COMPLETED`, `FAILED`, `CANCELLED`, `BLOCKED`) и attempt outcome (`PREPARED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`). Provider timeout может иметь неизвестный cost, но не создаёт внешний маркетинговый эффект и поэтому обрабатывается иначе, чем delivery `UNKNOWN`.

## Alternatives

- Каждый model call как отдельная Task: теряется единый budget и logical result identity.
- Retry мутирует прежний AgentRun: нельзя восстановить расходы и причину каждой попытки.
- Поздний result принимается после cancel: нарушает human control и version integrity.

## Consequences

A1 вводит contract/таблицы additive; legacy Task/AgentRun остаются writer до cutover. Нужны concurrent claim, budget exhaustion, cancel race, duplicate delivery и late-result tests.

## Validation / links

Закрывает Q5 в [roadmap](../architecture-v2/roadmap.md). Связано с [domain map](../architecture-v2/domain-map.md), [entity model](../architecture-v2/entity-model.md) и [ADR 005](005-transactional-outbox.md).
