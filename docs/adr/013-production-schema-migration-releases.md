# ADR 013: production-релизы с изменением схемы

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: release architecture для перехода от schema-static deploy к контролируемым forward migrations.

## Context

Production сейчас работает на Alembic `20261009_0028`. После A1 foundation ветка `main` содержит additive migrations:

- `20261121_0029` — таблицы `workspaces` и `workspace_memberships`;
- `20261121_0030` — deterministic default workspace и membership для существующих активных пользователей.

Действующий release protocol намеренно разрешает только образы с неизменной схемой `20261009_0028`. PR71 добавил ранний schema compatibility gate: migration-image блокируется до manifest и approval. Это правильное fail-closed поведение, но оно означает, что A1 foundation нельзя разворачивать, пока не принят отдельный migration-capable protocol.

Простое ослабление сравнения Alembic head недопустимо. При изменении схемы нужно доказать:

- точный source и target schema head;
- совместимость target image с планом миграции;
- успешный upgrade на production-подобной копии;
- сохранность существующих business-data;
- границу, до которой допустим автоматический downgrade;
- поведение при сбое до и после запуска writers;
- отсутствие blind restore или silent failback.

## Decision

### 1. Migration release — отдельный тип release

Release manifest для изменения схемы обязан содержать:

- `source_release_sha` и `target_release_sha`;
- immutable image digests;
- `source_schema_head`;
- `target_schema_head`;
- упорядоченный migration path;
- hash migration plan;
- migration compatibility class;
- pre-activation rollback policy;
- exact rehearsal proof ID;
- expected current production BUILD_SHA и schema head.

Schema-static и migration release не считаются одним режимом. Оператор не может превратить обычный manifest в migration manifest CLI-флагом.

### 2. Только явный forward upgrade

Штатное production-изменение схемы выполняется только через:

```text
source_schema_head
→ проверенный ordered migration path
→ target_schema_head
```

Запрещены:

- `alembic stamp` как способ обхода;
- ручной SQL business-state edit;
- выбор `head` без exact expected target;
- пропуск неизвестной revision;
- автоматический переход через несколько непроверенных heads;
- использование локально собранного backend image.

`apply` перед остановкой runtime повторно сверяет, что production schema точно равна `source_schema_head`.

### 3. Expand-first compatibility

Первый migration protocol поддерживает только additive/expand изменения:

- новые таблицы;
- новые индексы и constraints, доказанно совместимые с текущими данными;
- nullable/additive columns с безопасным backfill;
- deterministic derived rows, не меняющие прежний business state.

В первом protocol запрещены:

- drop/rename используемых таблиц или колонок;
- destructive type conversion;
- изменение смысла существующего статуса;
- массовая перезапись исторического business state;
- provider side effects;
- migration, требующая одновременной работы двух writers.

Contract/cleanup migrations выполняются только позднее, после отдельного cutover и compatibility window.

### 4. Отдельная migration rehearsal

До approval target migration release проходит isolated rehearsal:

1. Берётся проверяемый encrypted backup/fixture на source schema.
2. Restore выполняется в новый PostgreSQL с `network=none` и без production ports.
3. Проверяются source schema, source row counts и business-data hash.
4. Подписанный target backend image запускает exact upgrade до `target_schema_head`.
5. Выполняются `alembic current`, `alembic check` и migration-specific invariants.
6. Поднимается target runtime без production credentials и внешней сети.
7. Проверяются health/readiness, BUILD_SHA, topology и отключённые provider gates.
8. Проверяется pre-activation downgrade, если он заявлен разрешённым.
9. После downgrade должны восстановиться source schema и исходный hash всех существовавших до migration business tables.
10. Disposable project, volumes, backup и keys удаляются; сохраняется только безопасный proof.

Rehearsal использует exact source/target image digests из будущего manifest, а не код checkout.

### 5. Production-порядок

Migration `apply` выполняется под operation lock и только после нового manual production approval.

Порядок:

```text
read-only inventory и quiet gate
→ encrypted backup + isolated restore proof
→ stop schedulers
→ drain workers
→ повторный quiet gate
→ stop all application writers
→ exact source schema check
→ target image migration preflight
→ alembic upgrade exact_target
→ schema и data invariant checks
→ target backend в maintenance/readiness mode
→ frontend readiness
→ workers/schedulers по очередям
→ postflight
→ снять maintenance mode
```

До завершения postflight publishing providers не включаются и новые claims не разрешаются.

### 6. Две разные rollback boundary

#### До активации writers

Автоматический downgrade допустим только если одновременно выполнены все условия:

- migration manifest явно разрешает `PRE_ACTIVATION_DOWNGRADE`;
- migration rehearsal доказал downgrade exact target → exact source;
- после upgrade не запускались workers/schedulers и не было application writes;
- target runtime не вышел из maintenance mode;
- existing business-data hash не изменился;
- downgrade возвращает exact source schema и проходит `alembic check`.

После подтверждённого downgrade возвращаются exact previous image IDs и source BUILD_SHA.

#### После активации writers

Автоматический schema downgrade запрещён.

При ошибке после начала writers:

- новые claims блокируются;
- runtime переводится в maintenance/fail-closed;
- фиксируются известные attempts и UNKNOWN outcomes;
- выполняется только отдельно определённый fail-forward/operator recovery.

Backup не восстанавливается поверх живой БД автоматически. Возврат старых application images допустим только при отдельно доказанной совместимости со схемой target; сам факт additive migration этого не доказывает.

### 7. Approval должен явно показывать изменение схемы

Review artifact и signed receipt показывают минимум:

```text
source release SHA / schema
→ target release SHA / schema
migration revisions
compatibility class
pre-activation rollback policy
rehearsal proof
```

Approval обычного schema-static release нельзя переиспользовать для migration release. Approval одноразовый и расходуется до первой production mutation, как и в действующем protocol.

### 8. Migration-specific invariants обязательны

Одних `alembic upgrade` и HTTP readiness недостаточно. Каждая migration добавляет machine-checkable invariants.

Для первого перехода `20261009_0028 → 20261121_0030` обязательны:

- exact canonical workspace ID `4183ac32-0f61-54da-ad9e-ebb31696101b`;
- name `iTeam`, slug `iteam`, `is_default=true`, `is_active=true`;
- ровно один default workspace;
- membership каждого активного пользователя;
- `ADMIN → WORKSPACE_ADMIN`;
- `MANAGER → MARKETING_MANAGER`;
- отсутствие membership для inactive users;
- отсутствие изменений users, campaigns, publications, approvals и audit history;
- downgrade удаляет только canonical workspace/memberships и не удаляет пользователей.

### 9. Release state и audit

Локальный private state сохраняет:

- source/target manifest;
- schema before/after;
- migration start/finish timestamps;
- hash проверяемых business tables до/после;
- результат каждого invariant;
- activation boundary;
- rollback/fail-forward result;
- consumed approval ID.

В state и proof не сохраняются credentials, backup keys, recipient data или provider tokens.

## First implementation sequence

После принятия ADR реализация разбивается минимум на отдельные PR:

1. **Manifest contracts** — source/target schema, migration plan hash и compatibility class; без production mutation.
2. **Isolated migration drill** — upgrade/downgrade/data invariants на disposable restore.
3. **Approval workflow** — review artifact и signed receipt для migration release.
4. **Apply pre-activation path** — lock, quiet gate, backup, stop/drain, exact upgrade, readiness и разрешённый downgrade до writer activation.
5. **Post-activation recovery controls** — maintenance/fail-forward, без automatic downgrade.
6. **First production acceptance** — отдельно разрешённый переход `0028 → 0030` с publishing disabled.

Подготовительная часть этапа 5 — [read-only inspection сохранённой операции](../migration-operation-inspection.md).
Она проверяет связность journal/marker/runtime/backup metadata и всегда оставляет
текущую writer boundary неизвестной. Эта часть не реализует activation или
post-activation recovery и не завершает этап 5.

Ни один PR не должен одновременно добавлять новую business schema и включать её production deployment.

## Alternatives

### Разрешить любой image head

Отклонено: подпись образа не доказывает безопасность его migration path или rollback.

### Всегда выполнять `alembic downgrade` при ошибке

Отклонено: после application writes downgrade может потерять или неверно интерпретировать данные.

### Всегда восстанавливать backup

Отклонено: restore поверх живой БД может удалить подтверждённые действия, возникшие после snapshot, и скрыть неоднозначные provider outcomes.

### Никогда не разрешать migration deploy

Отклонено: блокирует принятую additive архитектуру и переносит риск в будущий большой переход.

## Consequences

- Release tooling становится сложнее, но schema change получает явную trust boundary.
- Additive migrations можно разворачивать малыми проверяемыми шагами.
- Старый image rollback после writer activation больше не обещается без доказанной schema compatibility.
- Разработка новых migrations может продолжаться только при наличии rehearsal и migration-specific invariants.
- Production остаётся на `20261009_0028`, пока отдельный migration protocol не реализован, принят и одобрен.

## Non-goals

ADR не:

- выполняет deployment;
- одобряет переход `0028 → 0030`;
- изменяет release scripts/workflows;
- меняет production schema;
- включает VK или другие providers;
- выбирает CMS/email provider;
- разрешает destructive contract migrations.

## Validation / links

Связано с:

- [Migration strategy](../architecture-v2/migration-strategy.md);
- [Roadmap](../architecture-v2/roadmap.md);
- [Release operations](../release-operations.md);
- ADR 002, ADR 005, ADR 006 и ADR 007.

Принятие ADR является prerequisite для implementation PR migration-capable release tooling.
