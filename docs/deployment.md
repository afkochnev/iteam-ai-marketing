# MVP production deployment

The supported default deployment is the Compose topology: PostgreSQL, Redis,
FastAPI backend, Next.js frontend, an AI-only Celery worker and a separate AI
scheduler behind a TLS-terminating reverse proxy. Publication execution,
publication dispatch/recovery and metrics workers are isolated behind the
opt-in `publishing` Compose profile. Use managed PostgreSQL/Redis when available;
this repository does not provide cloud-specific infrastructure.

## Order

1. Provision PostgreSQL and Redis and wait for health checks.
2. Create a production `.env` with generated secrets and `APP_ENV=production`.
3. Build pinned images and run `alembic upgrade head` as a one-shot migration
   step. Do not let every application container migrate concurrently.
4. Start the default Compose services and verify `/health/ready`.
5. Verify `ai_worker` listens only to `ai`, while its environment contains no
   Telegram/VK credentials and publishing flags are false.
6. Start frontend/reverse proxy and verify TLS and browser access.
7. Enable `--profile publishing` only after separate publication approval and
   provider configuration. The AI worker and scheduler never consume or dispatch
   publication queues.

Production validation rejects placeholder secrets, insecure cookies, local-only
allowed hosts and missing OpenAI runtime settings. The backend uses configured
CORS origins and trusted hosts; TLS/HSTS belongs at the proxy (the backend also
emits HSTS when `APP_ENV=production`).

Use `scripts/smoke.sh` after deployment and `scripts/release_check.sh` before
release. AI worker/scheduler and opt-in publication services use restart policy
`unless-stopped`. On shutdown, send TERM and allow the backend and active worker
roles to finish their current lifecycle; Celery's standard graceful shutdown is
used.

## Rollback

Rollback the application image first when the schema is compatible. A database
downgrade is only appropriate after a tested backup and when the migration is
explicitly reversible; never assume destructive data migrations can be safely
downgraded. If migration fails, keep the new application version stopped,
restore or repair the database, and rerun migration verification.

The MVP is single-tenant, uses a process-local rate limiter, and has no
automatic infrastructure backup or external publication. Include these
constraints in the deployment review.
