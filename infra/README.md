# Infrastructure

Development infrastructure is defined in the root `docker-compose.yml`. Apply the existing database migrations as part of environment setup; a fresh database cannot pass readiness until it is at the current Alembic head.

`docker compose up -d` starts PostgreSQL, Redis, backend, frontend, `ai_worker`, `ai_control_worker`, and `ai_scheduler`. Once an AUTO task becomes READY, the AI scheduler dispatches it without a manual worker/Beat launch or `/tasks/{id}/run` request.

| Queue | Consumer | Startup |
| --- | --- | --- |
| `ai` | `ai_worker` | default |
| `ai_control` | `ai_control_worker` | default |
| `publication` | `publication_worker` | `publishing` profile |
| `publication_control` | `publication_control_worker` | `publishing` profile |
| `metrics` | `metrics_worker` | `publishing` profile |
| `ai_live_test` | separately managed test worker | no production service |
| `unrouted` | none | fail-safe for unknown/default tasks |

`ai_scheduler` schedules only AI dispatch and AI/indexing recovery. `publication_scheduler` schedules only publication dispatch, publication recovery and metrics sync, and requires the `publishing` profile. No worker embeds Beat. AI services disable publishing and clear Telegram/VK credentials; publishing services clear OpenAI credentials. Compose sets `WORKER_ROLE` and `SCHEDULER_ROLE`; leave those variables unset in API/producer processes. Startup rejects mixed roles or publication capability in an AI role without logging secret values.

The revision read-model uses Celery's existing queue-consumer inspection for executor availability. A queued run's age alone does not prove that its executor is unavailable; failed inspection returns unknown availability.

See [deterministic acceptance](../scripts/acceptance/README.md) for an isolated real-broker/worker/scheduler scenario with zero provider calls.
