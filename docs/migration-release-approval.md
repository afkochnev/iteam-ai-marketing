# Approval релиза с изменением схемы

Workflow **Approve migration release** реализует третий этап
[ADR 013](adr/013-production-schema-migration-releases.md): проверяемый review
artifact и подписанный receipt для отдельного release type `MIGRATION`.
Добавление workflow не запускает approval и не разрешает production migration.
Команды этого этапа проверяют artifacts и images, создают локальные файлы и
подпись; подключения к production runtime у них нет.

## Что нужно для подготовки

Сначала должен завершиться успешный CI на `main`, содержащий
[isolated migration rehearsal](migration-rehearsal.md). Допускается только
первая попытка этого workflow run. Нужны:

| Input | Значение |
| --- | --- |
| `rehearsal_run_id` | ID успешного `ci.yml`, event `push`, branch `main`, в этом repository |
| `rehearsal_artifact_id` | ID его artifact `migration-drill-proof` |
| `proof_sha256` | `sha256:` и SHA-256 **исходных байтов JSON proof**, а не ZIP archive |
| `expected_current_release_sha` | Ожидаемый production BUILD_SHA, совпадающий с source release в proof |
| `expected_current_schema_head` | Ожидаемая production schema, совпадающая с source schema в proof |
| `target_build_run_id` | Exact `Release images` run, уже связанный с rehearsal proof |

Source SHA и schema — явное ожидание оператора. Approval не считывает текущее
состояние production и не доказывает, что оно совпадает с этими inputs. Будущий
migration apply обязан проверить оба значения повторно до первой mutation.

Request берётся из закреплённого
`scripts/fixtures/migration-0028-to-0030.json` на точном commit rehearsal run.
Поэтому этот этап поддерживает проверенный переход `20261009_0028 →
20261121_0030` и конкретную пару подписанных source/target images. Новый build,
другая попытка build или иной image digest требуют соответствующей репетиции.
Proof на синтетической fixture не является проверкой актуальной production БД.

## Проверки до ручного approval

`scripts/migration_release_approval.py prepare` проверяет:

- доверенный успешный CI run, его repository, branch, event, workflow path,
  первую попытку и принятие exact run head в `main`;
- exact artifact ID и его принадлежность этому run/head, имя, срок хранения,
  checksum ZIP и SHA-256 исходного JSON;
- полный contract proof: exact source/target SHA, schema и images, target build
  run/attempt, migration plan, revisions, readiness, invariants, восстановление
  из encrypted backup, сохранность business-data и успешный cleanup;
- допустимую rollback policy: downgrade proof обязателен для
  `PRE_ACTIVATION_DOWNGRADE`, а `FAIL_FORWARD_ONLY` не обещает downgrade;
- отсутствие в rehearsal writers, production credentials, publishing и
  внешней сети;
- успешные CI для обоих release SHA, exact target build и его artifacts,
  подписи/provenance всех четырёх images, exact Alembic heads и hashes migration
  scripts внутри target backend image.

Timestamp proof должен попадать в интервал CI run. Дополнительного произвольного
лимита возраста rehearsal нет: artifacts должны оставаться доступными и
неистёкшими, а вся цепочка доверия проверяется заново. Отсутствующий, истёкший
или изменившийся artifact блокирует подготовку.

Review summary показывает source SHA/schema → target SHA/schema, immutable
images, ordered revisions и script hashes, plan hash, compatibility class,
rollback policy, target build и ссылку на конкретный proof. Artifact
`migration-release-for-review` содержит `migration-review.json` с manifest v2.
Это материал для проверки человеком; он ещё не является approval receipt.

Подготовку можно выполнить отдельно на runner с GitHub CLI, Docker, Buildx и
Cosign. Пример использует переменные, которые нужно заполнить проверенными
значениями:

```bash
python3 scripts/migration_release_approval.py prepare \
  --rehearsal-run "$REHEARSAL_RUN_ID" \
  --rehearsal-artifact "$REHEARSAL_ARTIFACT_ID" \
  --proof-sha256 "$RAW_PROOF_SHA256" \
  --expected-source-release "$EXPECTED_PRODUCTION_SHA" \
  --expected-source-schema "$EXPECTED_PRODUCTION_SCHEMA" \
  --target-build-run "$TARGET_BUILD_RUN_ID" \
  --output migration-review.json \
  --summary migration-review.md
```

Этот вызов не запускает workflow и не создаёт manual production approval.

## Ручное решение и signed receipt

Workflow запускается только явным `workflow_dispatch` на `main`; rerun
отклоняется. Он использует ту же concurrency group `production-release-approval`,
что и schema-static approval. Job `approve` зависит от успешного `prepare` и
защищён environment `production`. Reviewer сначала проверяет summary и review
artifact, затем принимает решение через environment gate.

После ручного решения signing job загружает **тот же artifact по ID** в
отдельную временную директорию и сверяет hash review bytes, переданный из
`prepare`. Команда `seal` повторно проверяет внешние доказательства, сравнивает
вновь собранный review с рассмотренным и связывает receipt с текущим workflow
run, attempt и SHA. Только после этого Cosign создаёт keyless signature.

Artifact `approved-migration-release` содержит:

- `migration-release.json` — receipt отдельного типа с полным migration review;
- `migration-release.sigstore.json` — Sigstore bundle для exact receipt bytes.

Identity подписанта закреплена за
`.github/workflows/approve-migration-release.yml@refs/heads/main` этого
repository и GitHub Actions OIDC issuer. Receipt другого workflow, включая
`Approve release`, не подходит для migration release.

Read-only verifier дополнительно проверяет актуальные branch/environment
protections, историю явного решения пользователя `afkochnev`, завершение
approval run с успехом, первую попытку, подпись и всю цепочку manifest/proof.
Срок пригодности receipt — не более одного часа после завершения approval run,
как в действующем schema-static protocol. Проверка требует token с правами
чтения соответствующих GitHub protection settings; обычный `GITHUB_TOKEN`
prepare job не используется для этих административных чтений.

```bash
python3 scripts/migration_release_approval.py verify-approved \
  --approval-run "$APPROVAL_RUN_ID" \
  --approval-artifact "$APPROVAL_ARTIFACT_ID" \
  --output verified-migration-receipt.json
```

Эта команда только проверяет уже существующий receipt. Она не запрашивает
approval и не выполняет production mutation.

## Проверка в CI

Обычный CI вызывает только `prepare` против закреплённого успешного rehearsal
run на `main`. Он сохраняет результат как `migration-approval-check`, проверяя
на настоящем artifact его идентичность, содержимое, hashes и всю цепочку
подписанных images. CI не вызывает `seal`, не запускает approval workflow и не
запрашивает environment approval. Unit tests отдельно проверяют отказы при
подменах и нарушениях границ workflow.

Закреплённый CI proof относится к предыдущему успешному main run. При истечении
его срока хранения или смене fixture нужно намеренно выбрать и проверить
новый успешный main rehearsal, затем обновить точные run/artifact IDs и raw
JSON hash в CI. Автоматическая подмена на «последний» proof не выполняется.

## Граница следующего этапа

Существующие `release_ops.py prepare/apply` сохраняют schema-static contract v1
и gate `20261009_0028`. Они не принимают новый migration receipt. Этот этап не
добавляет migration apply, не подключается к production, не выполняет upgrade,
downgrade, restore production backup или запуск writers.

Одноразовое расходование approval реализуется в будущем migration apply:
под operation lock, после повторной read-only проверки ожидаемых source
SHA/schema и до первой production mutation. Проверка receipt сама по себе
не расходует approval и не разрешает автоматический retry. После активации
writers автоматический schema downgrade остаётся запрещённым по ADR 013.
