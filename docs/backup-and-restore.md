# Backup and restore

PostgreSQL is the source of truth for campaigns, tasks, agent runs, provenance,
approvals and operational history. For the MVP, take an encrypted `pg_dump`
at least daily and retain several rotation points according to the hosting
policy. Store backups outside the database host and restrict access to
operations administrators.

```bash
pg_dump --format=custom --file=iteam-$(date +%Y%m%d-%H%M).dump "$DATABASE_URL"
```

The dump contains application metadata and audit state, but not bytes held by
OpenAI Files or Vector Stores. Preserve `OPENAI_VECTOR_STORE_ID` and knowledge
source metadata separately. If the external vector store is lost, re-upload
the original documents through the Knowledge UI; the current MVP does not
retain a local copy of uploaded files.

## Restore exercise

1. Provision an empty PostgreSQL database and credentials.
2. Restore with `pg_restore --clean --if-exists` into that database.
3. Run `alembic upgrade head` and verify `alembic check`.
4. Start backend, worker, Beat and frontend only after migration succeeds.
5. Check `/health`, `/health/ready`, admin login, task/approval lists and a
   read-only campaign page.
6. Reconcile the configured OpenAI Vector Store and re-index documents if the
   external store was not preserved.

Perform a restore exercise before relying on a backup policy; a successful dump
alone is not proof of recoverability. Never put credentials in commands saved
to shell history or in this document.
