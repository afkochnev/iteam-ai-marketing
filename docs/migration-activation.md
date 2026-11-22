# Migration apply с явной активацией runtime

[`migration_activation_apply.py`](../scripts/migration_activation_apply.py)
добавляет production-capable controller этапа 5
[ADR 013](adr/013-production-schema-migration-releases.md). Он выполняет новую
операцию от source inventory до активации target runtime под одним lock и с
отдельным signed approval. Первый поддерживаемый переход схемы —
`20261009_0028 → 20261121_0029 → 20261121_0030`.

Request фиксирует rehearsed source release
`f0913e0029e25da58ea95504fd07b0989811dbb9` со schema `20261009_0028`.
Это ожидаемая source identity; текущий production inventory в рамках этого
изменения не снимался. Plan и apply обязаны сверить её с фактическим runtime.

Этот документ описывает реализацию и порядок будущего выполнения. Он не
фиксирует результат нового CI или production-приёмки. До выдачи approval
требуется успешный, принятый CI exact `main` с combined signed-image proof.
Production approval, deployment и migration в рамках этого изменения не
выполнялись. Первая production-приёмка остаётся отдельно разрешаемым этапом 6.

## Отдельные полномочия

| Протокол / evidence | Полномочия |
| --- | --- |
| Прежний migration approval v1 и `migration_release_apply.py` | Только pre-activation path; завершение `PRE_ACTIVATION_READY` не активирует приложения |
| Candidate admission proof | `DISPOSABLE_CANDIDATE_ONLY`; локальный candidate image и собственный fixture |
| Combined signed-image rehearsal | `DISPOSABLE_SIGNED_ACTIVATION_ONLY`; подписанные release images и собственные fixtures |
| Новый review/receipt v2 | Явное `PRODUCTION_MIGRATION_ACTIVATION` для одной новой операции и конкретного исходного runtime |

Nested migration manifest сохраняет существующий contract версии 2. Новый
approval не меняет смысл старого v1 receipt, а новый controller не продолжает
сохранённую операцию stage 4. Если отдельный pre-activation apply уже завершился,
его журнал и поздняя инспекция не восстанавливают непрерывный live context.
Команды resume, force, ручного перехода через marker или downgrade здесь нет.

Фиксированная [activation policy](../scripts/migration_activation_contract.py)
явно разрешает **business writes, queue claims и OpenAI requests** через точную
прежнюю capability configuration после durable boundary. Publication requests
и все metrics requests остаются запрещёнными. Проверяются выключенные
`VK_PUBLISHING_ENABLED`, `TELEGRAM_PUBLISHING_ENABLED`, `METRICS_SYNC_ENABLED`,
`VK_METRICS_ENABLED` и `TELEGRAM_METRICS_ENABLED`.

Поэтому production authorization не описывается как запрет любых внешних
side effects: OpenAI capability входит в review и receipt. Выключенные
publication/metrics flags и passive admission имеют разные задачи; одни flags
не запрещают business writes и не заменяют passive mode.

## Два разных checkout

Инструмент требует два отдельных абсолютных пути:

| Путь | Обязательная identity |
| --- | --- |
| `CONTROLLER_ROOT` | Чистый checkout exact commit успешного rehearsal CI на `main`; его SHA/tree связаны с combined proof |
| `RUNTIME_ROOT`, передаваемый как `--root` | Чистый production checkout exact `source_release_sha`; `.env`, OCI revisions и запущенные image IDs согласованы с source manifest |

Controller запускают из checkout, который действительно исполнил combined
rehearsal. Произвольный более новый `main` не заменяет этот checkout. SHA
контроллера, target image release SHA и SHA signing workflow имеют отдельные
роли и могут различаться. Target images берутся по digest из проверенного
[request](../scripts/fixtures/migration-activation-0028-to-0030.json), а не
собираются из controller checkout.

В Git object database `RUNTIME_ROOT` уже должны быть доступны exact target
commit и его `docker-compose.yml`: adapter читает
`git show <target_release_sha>:docker-compose.yml` и сам не выполняет fetch.
При отсутствии target object/file plan завершается до расходования approval.

Production `.env` имеет owner текущего оператора и mode `0600`, source
`BUILD_SHA` и `APP_ENV=production` либо `prod`. Общий private state фиксирован:
`~/iTeam-private/release-operations/iteam-ai-marketing`; CLI не принимает
альтернативный `--state-dir`. State и отдельные backup/key directories
принадлежат тому же оператору и имеют mode `0700`; ciphertext и key хранятся
раздельно, вне runtime checkout. Plan output также сохраняют вне обоих
checkout, в private directory, по ещё не существующему пути.

Нужны Docker/Compose, Git, GitHub CLI и Cosign, доступ к доверенным GitHub/GHCR
evidence и локальному production Docker daemon. Проверка evidence заново
проверяет подписи и загружает exact source/target digests в image cache.
Сам runtime adapter использует эти образы с `--pull=never`; изменение source
контейнеров, конфигурации, сети или database identity после plan запрещает
расходование receipt. Предварительные source quiet/topology ограничения
[этапа 4](migration-preactivation-apply.md) сохраняются.

Поддерживаемая topology требует, чтобы каждая сеть приложений и Redis была
объявлена в Compose и использовала network ID, уже связанный с исходным
PostgreSQL в inventory. Отдельные сети только для приложений не поддерживаются.
Лишние подключения, подмена сети под тем же именем, расширение aliases/static
IPAM и расхождение CPU/memory с Compose отклоняются до расходования approval.

## 1. Read-only production plan

В примерах ниже переменные обозначают проверенные оператором значения.
`REHEARSAL_RUN` — успешный first-attempt CI на `main`, содержащий artifact
`migration-activation-drill-proof`; `REHEARSAL_ARTIFACT` — его exact ID.
`PROOF_SHA256` имеет формат `sha256:<64 lowercase hex>` и относится к исходным
bytes файла `migration-activation-drill-proof.json`, а не к пересохранённому JSON.
`TARGET_BUILD_RUN` — exact Release images run из request.

```bash
python3 "$CONTROLLER_ROOT/scripts/migration_activation_apply.py" plan \
  --root "$RUNTIME_ROOT" \
  --backup-dir "$BACKUP_DIR" \
  --key-dir "$KEY_DIR" \
  --rehearsal-run "$REHEARSAL_RUN" \
  --rehearsal-artifact "$REHEARSAL_ARTIFACT" \
  --proof-sha256 "$PROOF_SHA256" \
  --expected-source-release "$EXPECTED_SOURCE_SHA" \
  --expected-source-schema "$EXPECTED_SOURCE_SCHEMA" \
  --target-build-run "$TARGET_BUILD_RUN" \
  --cosign "$COSIGN_BIN" \
  --output "$PLAN_OUTPUT"
```

Plan проверяет доверенный CI, request, artifact hash, signed images и admission
protocol 1. Под общим lock он читает source inventory, quiet state и будущую
target configuration. Он создаёт новый 32-character lowercase hex
`operation_id` и `expected_runtime_identity_sha256`, а также сообщает manifest,
target-plan и evidence hashes. Runtime identity включает operation ID,
manifest, source container/image identities, restart state/policies, topology,
configuration/environment hashes и database identity.

Plan создаёт private локальные служебные файлы, но не останавливает production
runtime, не меняет schema и не выдаёт approval. Его поля
`production_mutation_performed`, `production_approval_verified` и
`activation_authorized` равны `false`. Lock освобождается после завершения
read-only планирования; ожидание человеческого решения не удерживает runtime.

## 2. Новый review и ручной approval

[Approve migration and activation](../.github/workflows/approve-migration-activation.yml)
запускается вручную на `main`. Передаются те же evidence/source/build values,
а также exact `operation_id` и `expected_runtime_identity_sha256` из plan.
Пример запуска workflow после отдельного решения о выдаче approval:

```bash
gh workflow run approve-migration-activation.yml \
  --repo afkochnev/iteam-ai-marketing --ref main \
  -f "rehearsal_run_id=$REHEARSAL_RUN" \
  -f "rehearsal_artifact_id=$REHEARSAL_ARTIFACT" \
  -f "proof_sha256=$PROOF_SHA256" \
  -f "expected_current_release_sha=$EXPECTED_SOURCE_SHA" \
  -f "expected_current_schema_head=$EXPECTED_SOURCE_SCHEMA" \
  -f "target_build_run_id=$TARGET_BUILD_RUN" \
  -f "operation_id=$OPERATION_ID" \
  -f "expected_runtime_identity_sha256=$EXPECTED_RUNTIME_IDENTITY_SHA256"
```

Job `prepare` публикует `migration-activation-for-review` с
`migration-activation-review.json` и объясняет capability policy в summary.
До `approve` оператор проверяет source/target/schema, migration path, exact
digests, runtime binding и разрешение OpenAI. Job `approve` требует manual
решения environment `production`, получает review по exact artifact ID,
проверяет hash исходных canonical bytes, повторно проверяет evidence и только
после этого подписывает receipt через Cosign.

Artifact `approved-migration-activation` содержит:

- `migration-activation.json` — receipt версии 2;
- `migration-activation.sigstore.json` — signature bundle.

Receipt связывает review hash, approval run/attempt, workflow SHA и явное
`PRODUCTION_MIGRATION_ACTIVATION`. Подписанные `issued_at` и `expires_at`
ограничивают срок максимум **3600 секундами**. Отсчёт идёт от sealing после
повторной проверки, а не от начала ожидания human gate. Verifier проверяет
first attempt, trusted successful workflow, production decision указанного
человека, защиты repository/environment, signature identity именно нового
workflow, artifact bytes и свежесть после повторной проверки evidence.

Workflow не подключается к production. Значения source/runtime в review —
проверяемые assertions оператора из plan; окончательное сравнение делает
controller на host до расходования approval.

## 3. Одна новая migration-and-activation operation

После отдельного разрешения этапа 6 оператор использует exact run и artifact
нового approval, тот же controller checkout и тот же runtime root:

```bash
python3 "$CONTROLLER_ROOT/scripts/migration_activation_apply.py" apply \
  --root "$RUNTIME_ROOT" \
  --backup-dir "$BACKUP_DIR" \
  --key-dir "$KEY_DIR" \
  --approval-run "$ACTIVATION_APPROVAL_RUN" \
  --approval-artifact "$ACTIVATION_APPROVAL_ARTIFACT" \
  --cosign "$COSIGN_BIN"
```

Apply держит общий exclusive `operation.lock` от проверки receipt до закрытия
наблюдателей. Fresh inventory обязан совпасть с подписанным runtime hash;
target plan проверяется до `Journal.consume()`. Расходуется общий одноразовый
`approval-<run>.consumed`. Существующая operation, activation journal или
activation marker блокируют новую попытку; новый approval не обходит эту
проверку.

Внутри одного процесса последовательно выполняются:

1. Прежний pre-activation engine: quiet, encrypted backup/isolated restore,
   stop/drain, fencing, exact upgrade и data/schema checks. Он заканчивает
   `PRE_ACTIVATION_READY`, сохраняя source writers остановленными.
2. Passive target preflight при живой source barrier: production validators,
   readiness с read-only DB, HTTP `503 RUNTIME_PASSIVE`, отказ Celery/beat до
   consumer bootstrap, полный неизменный target snapshot и удаление helpers.
3. Handoff наблюдения и durable one-way boundary: intent, exclusive `.started`
   marker, file/directory `fsync`, readback и подтверждённый activation journal.
4. Cold recreation всех девяти application services из exact target images с
   `restart=no`, восстановление только связанных data networks и запуск
   backend/frontend, exact workers и schedulers.
5. Active postflight: build/schema/admission readiness, container/configuration
   identity и настоящие Celery queue registrations. После успешного postflight
   восстанавливаются exact прежние service restart policies, конфигурация
   проверяется снова и записывается terminal `ACTIVE`.

**Target claims запрещены до durable boundary. После неё запуск ACTIVE workers
уже разрешает claims; проверка их queue registrations выполняется после запуска.**
Утверждать отсутствие claims до конца active postflight было бы неверно.
Business writes и OpenAI calls также могут начаться после разрешённого запуска
ACTIVE runtime. Publication и metrics gates остаются выключенными на всём пути.

Каждая разрешающая проверка связана с исходным live объектом/process и exact
записями. Wall-clock и monotonic deadline запрещают дальнейшие active mutations
после истечения receipt. Истечение срока или повреждение state не запрещают
stop/fence. Восстановление normal restart policies относится к штатной работе
успешно принятого runtime; `automatic_retry=false` относится к самой операции.

## Сбой, durable state и отсутствие resume

После возврата прежнего engine controller больше не входит в его downgrade
handler. Ошибка passive preflight, handoff, activation, active postflight или
финальной записи ведёт к попытке физического containment. После durable
boundary source runtime не запускается, target schema не понижается, backup
поверх БД не восстанавливается.

Adapter независимо пытается убрать helpers, отключить restart policies,
остановить известные writers и отсоединить PostgreSQL/Redis от сетей. Проверки
включают прежний host port, отсутствие клиентов, database identity, exact
target schema и стабильный полный target snapshot. Ошибка journal, потеря
observer или неуспех одного шага не должны препятствовать оставшимся
физическим попыткам остановки и изоляции.

| Итог controller | Смысл |
| --- | --- |
| `ACTIVE` | Active postflight, конфигурация, normal restart policies, закрытие observers и exact возвращаемый durable state подтверждены |
| `CONTAINED` | Операция не завершилась успешно; runtime остановлен, новые claims закрыты, data services изолированы, target schema/identity сохранены и state подтверждён |
| `OPERATOR_RECOVERY_REQUIRED` | Полный результат, cleanup или достоверность state подтвердить нельзя; положительные assertions не восстанавливаются по позднему чтению |

К существующим четырём документам pre-activation добавляются
`activation-<operation-id>.json` и `activation-<operation-id>.started`.
Они связывают receipt, operation/runtime, manifest/target, policy, срок,
pre-activation records и target plan. Private вспомогательные Compose/plan
файлы фиксируют ожидаемую конфигурацию; они не являются execution authority.
Credentials, raw Docker environment, backup keys и business rows в безопасные
journals/reports не записываются.

Старый `operation-<id>.json` остаётся историческим `PRE_ACTIVATION_READY` даже
после последующей активации. Поэтому результат
[старого inspector](migration-operation-inspection.md) не является итогом
полной операции: он не читает новый activation journal и всегда возвращает
`writer_boundary=UNKNOWN`. Новый journal также не имеет load/resume API.

При прерывании сохраняют journal/markers, runtime/plan metadata, backup proof,
ciphertext и отдельно ключ. Процесс, потерявший наблюдение, и новый процесс
не получают полномочий из этих файлов. Удаление state, смена пользователя,
новый approval или повтор команды не являются recovery. Дальнейшее решение
требует отдельной проверки фактической БД/runtime и отдельно разрешённого плана.

## Combined signed-image rehearsal

Job `migration-activation` в [CI](../.github/workflows/ci.yml) вызывает
[`migration_activation_drill.py`](../scripts/migration_activation_drill.py)
на отдельном Docker runner с минимум 6 GiB памяти. Он не принимает production
root, credentials или namespace и публикует proof только после cleanup.

Proof объединяет пять проверяемых частей:

1. Исходный migration rehearsal: backup/restore, exact upgrade, workspace
   invariants и допустимый downgrade.
2. Явное подтверждение, что gate-capable target в этой вложенной rehearsal
   находился в PASSIVE/read-only режиме **до** допустимого downgrade.
3. Отдельный signed-target adversarial passive fixture с семью реальными
   queued canaries, entrypoint matrix, provider witness и непрерывным Redis
   monitor. Полный snapshot покрывает **все 37 public tables** target schema,
   включая `alembic_version`, `workspaces` и `workspace_memberships`.
4. Успешный новый путь того же controller от source quiet до `ACTIVE`.
5. Отдельный путь с реальным HTTP commit и worker claim после boundary,
   внедрённым последующим отказом и физическим containment. Полный hash всех
   37 таблиц после подтверждённой записи должен сохраниться после containment;
   downgrade/restore/source restart запрещены.

Adversarial queued fixture отделён от production-like source quiet fixture.
Source quiet gate не ослабляется: work не добавляется перед quiet и между
`PRE_ACTIVATION_READY` и durable boundary. В end-to-end сценарии реальная
работа создаётся только после разрешённой активации.

Application containers и Redis используют internal networks; disposable
PostgreSQL дополнительно имеет отдельную host-publish bridge для проверки
настоящего ephemeral port на `127.0.0.1`. Поэтому proof отдельно утверждает
`application_external_network=false` и `postgres_has_host_publish_network=true`.
После containment прежний host port обязан быть недоступен.

Combined proof связывает точный request, source/target digests, build run,
schemas/migration plan, policy и фактический clean controller SHA/tree. Он
содержит только `DISPOSABLE_SIGNED_ACTIVATION_ONLY`,
`production_approval_verified=false` и отсутствие production credentials.
Его успешный результат становится входом будущего manual review; production
authority выдаётся только отдельным workflow v2 и расходуется только новым
production apply. Этап 6 в этом изменении не выполняется.
