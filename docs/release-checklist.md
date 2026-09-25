# MVP release checklist

- [ ] Generate/rotate production secrets; set `APP_ENV=production` and
      `COOKIE_SECURE=true`.
- [ ] Configure database, Redis, OpenAI API/model/vector store and explicit
      frontend origin/allowed hosts.
- [ ] Take and test a PostgreSQL backup.
- [ ] Run `alembic upgrade head` as the one-shot migration step.
- [ ] Run `scripts/release_check.sh` and build all images.
- [ ] Verify admin bootstrap, seeded agents/tools, and configured prompts.
- [ ] Start backend, worker and Beat; verify `/health`, `/health/ready`, task
      registration and both Beat schedules.
- [ ] Start frontend behind TLS/reverse proxy; verify security headers.
- [ ] Run `scripts/smoke.sh` and the mocked E2E suite.
- [ ] Confirm no external publishing integration is enabled.
- [ ] Record rollback image, backup location and on-call owner.
