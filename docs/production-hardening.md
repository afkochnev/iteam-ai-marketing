# MVP 2 production hardening runbook

## Readiness and deployment

`/health` only proves that the process is alive. `/health/ready` additionally
checks PostgreSQL, Redis, and that `alembic_version` is exactly
the current Alembic head (`20260930_0020` for this release). A schema mismatch
is an operational stop condition; the application never runs migrations
implicitly.

Deploy in this order:

1. Take and verify a PostgreSQL backup.
2. Deploy compatible images.
3. Run `alembic upgrade head` once.
4. Verify `/health/ready`.
5. Start worker and Beat and verify registered tasks.
6. Start the frontend and run the read-only smoke checklist.

Never downgrade a database containing rows written by a newer application
without a tested backup and rollback plan.

Migration downgrade/upgrade round-trips are test-database-only operations. Do
not run `alembic downgrade base` or an equivalent reset against runtime or
production: a downgrade can drop audit/history tables, and a later upgrade
only recreates the schema—it cannot restore the deleted rows. Production
deployments use forward `alembic upgrade head` after backup and review.
For a local/CI round-trip, use `scripts/migration_roundtrip.sh`; it refuses
databases whose name does not end in `_test`.

## Failure handling

- A publication left in `PUBLISHING` is recovered to channel-specific
  reconciliation-required state. Never resend it blindly.
- A reconciliation-required publication must be confirmed externally before a
  retry is allowed.
- Redis or worker outages do not imply provider success. Inspect AgentRun or
  Publication state and enqueue only through the existing idempotent path.
- Feedback analysis remains advisory. Failed or ambiguous AgentRuns are not
  automatically accepted or applied to campaigns.
- Metrics failures are observational and never change Publication delivery
  state.
- Provider credentials may be disabled independently. Reconciliation remains
  available, while publishing is rejected clearly.

## Backup and restore

PostgreSQL is authoritative for content, provenance, approvals, publications,
metrics, feedback, AgentRuns and audit history. Redis is a delivery mechanism,
not a source of truth. Back up PostgreSQL with `pg_dump`, restore into an empty
database with `pg_restore`, then run `alembic upgrade head` and `alembic check`
before starting workers. OpenAI Vector Store identifiers and source-document
retention are separate from the database backup.

## Observability

The admin system status read model exposes task/AgentRun failures, stale work,
failed/reconciliation-required publications, metrics-sync failures and failed
feedback analyses. Correlate events using the safe IDs in ActivityLog; content
bodies and credentials are not operational log fields.

## Retention and immutability

Publications, immutable ContentVersions, ContentDerivation, reconciliation
history, metrics snapshots, feedback analyses, ActivityLog and AgentRun rows
are retained as operational history. Reconciliation and audit rows are
append-only; corrections create a new decision/event rather than rewriting
the old one. Metrics snapshots are append-only with identical observations
deduplicated. No normal operator endpoint deletes delivery facts or
provenance.

## Provider limitations

Automatic Telegram metrics are unsupported by the current Bot API integration.
Automatic VK metrics are unsupported with the configured community-token auth
model. Manual metrics remain available and are labelled as manual observations.
