# Autonomous runtime acceptance

Run from the repository root with Docker Compose v2 and the backend development dependencies installed:

```sh
backend/.venv/bin/python scripts/acceptance/run.py --full-backend
```

The harness starts an isolated project using the production Compose services and Dockerfiles, with temporary build-only proxy trust when configured. It publishes random loopback ports, uses private project volumes and an internal network, and supplies only synthetic configuration. Publishing profile services are never started. The project, its volumes and its image tags are removed even when a check fails; shared containers and volumes are untouched.

Only the AI worker's model boundary is replaced. Redis delivery, PostgreSQL coordination, worker claim, source-reading tool, result processor and terminal transitions remain real. The scenario creates an approved source, a READY `CONTENT_REVISION`, and an approved due publication. It holds the claimed run while multiple scheduler intervals and duplicate deliveries pass, then completes the fake result and repeats delivery after completion. Assertions require one run, one result version per item, and unchanged publication state with empty publication/control/metrics queues. An acceptance-only HTTP guard rejects real provider requests in every Python service; custom in-process transports in pytest remain allowed.

The runtime broker uses DB2 of the project's own Redis. Full pytest uses a separate PostgreSQL database ending in `_test`, Redis DB15 for health checks, and the application's existing memory Celery transport. Redis DB1 must remain empty. No user runtime Redis database or business data is accessed.

The result is saved to `/tmp/iteam-d4-acceptance-report.json` with UUIDs and counts, without credentials. This is deterministic acceptance; live OpenAI/Telegram/VK acceptance is deliberately excluded.
