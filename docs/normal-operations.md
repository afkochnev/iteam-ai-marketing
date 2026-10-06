# Normal operations

Only two runtime modes are supported by the operational entrypoint:

- `scripts/runtime-up.sh normal`: starts core, AI, publishing and metrics through the publishing profile. This authorizes provider execution according to the existing runtime configuration. Review configuration before using this mode; do not expose credential values.
- `scripts/runtime-up.sh maintenance`: stops all workers/schedulers, then starts core only. Wait for in-flight work first; ambiguous delivery requires reconciliation rather than retry.

Use `scripts/runtime-status.sh` for read-only container state/health. It does not print configuration. On Mac, Docker Desktop must start at login; the application cannot start a stopped Docker daemon. `unless-stopped` recovers services after Docker restart, but intentionally stopped containers require an explicit normal-mode start. PostgreSQL/Redis keep the existing named volumes. Never use `down -v` or replace the database with an empty instance. Deploy/migrate approved releases separately; these scripts do not migrate, seed, or rotate credentials.

## Workflow

READY task → autonomous AI execution → content/revision → human approval of exact version → scheduled Publication → automatic due dispatch → delivery or operator-visible failure. Work through campaigns/content/publications UI; no Codex intervention is needed for normal scheduling, cancellation or reconciliation.

AI consumes only ai/ai_control and has publishing credentials disabled. Publishing/metrics consume only publication/publication_control/metrics and have OpenAI credentials disabled. Each scheduler has its own schedules. Provider enabled does not mean approval or worker readiness; the dashboard reports data/capabilities, not an inferred heartbeat.

## Downtime and overdue slots

`PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS` defaults to 3600 (positive seconds). The dispatcher takes only approved, unclaimed, failure-free SCHEDULED rows in `[now - window, now]` for an enabled channel. Older rows remain SCHEDULED, without provider calls or silent state changes. The API computes `is_overdue` and elapsed seconds; no schema migration/status is introduced.

Open `/publications#overdue`, check campaign/channel/exact version/planned time and choose the existing human action in the campaign: publish now, cancel, or reschedule when permitted. Plan-bound channel/time stays authoritative; edit the mediaplan instead of overriding it. Publish-now still validates human authorization, exact approved version and plan snapshot; it does not substitute a newer version. No blind retry or provider-side cleanup. PUBLISHING recovery and reconciliation remain the existing contracts.

Dashboard warnings cover overdue, failed/reconciliation, old READY AI tasks and scheduled rows with disabled providers. READY age is a warning, not proof that a worker is stopped. Check runtime-status for actual container state.
