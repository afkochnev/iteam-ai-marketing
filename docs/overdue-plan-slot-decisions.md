# Overdue approved plan slots

An approved publication-plan slot can become overdue before a `Publication` row is created. The canonical operator workspace is `/publications`.

For an approved social post bound to an overdue `PLANNED` item, the operator must choose one explicit action:

- **Reschedule** — select a timezone-aware future time. The application creates a `SCHEDULED` Publication for the exact approved content version and records the original and effective times in an append-only audit event.
- **Cancel slot** — the application creates a terminal `CANCELLED` Publication at the original plan time. This action does not authorize or call a provider.

The approved publication plan and approved content version remain immutable. A rescheduled plan-bound Publication is eligible for delivery only when its effective schedule exactly matches the corresponding audit record. Any mismatch fails before provider access.

The operator card is rendered only after the content, plan and publication reads required for a safe decision have completed. Regression coverage verifies pre-Publication cancellation, future rescheduling, idempotency, immutable approved versions and rejection of unaudited schedule changes before provider access.

Do not repair these states through manual SQL or direct production database edits.
