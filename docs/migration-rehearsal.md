# Изолированная репетиция миграции

`scripts/migration_release_drill.py` реализует этап rehearsal из
[ADR 013](adr/013-production-schema-migration-releases.md). Команда проверяет
точный переход `20261009_0028 → 20261121_0030` через revisions
`20261121_0029`, `20261121_0030` на синтетической fixture. Она создаёт собственные
временные ресурсы Docker и не принимает production checkout, Compose project,
credentials или backup production как аргументы.

## Подготовка и запуск

Используйте отдельный runner с Docker Engine, Compose, Buildx, Python 3,
OpenSSL, GitHub CLI и Cosign. Docker должен видеть минимум **6 GiB** памяти.
Runner нужны права чтения GitHub Actions artifacts и подписанных GHCR images;
хосту требуется сеть для их проверки и загрузки. Все непустые переменные
`COMPOSE_*` должны отсутствовать: команда отклоняет overrides и явно закрепляет
абсолютные пути к сгенерированным Compose-файлам.

Из корня репозитория:

```bash
python3 scripts/migration_release_drill.py \
  --request /path/to/reviewed-migration-request.json \
  --output /path/to/new-migration-proof.json \
  --cosign cosign
```

Пути в примере — placeholders. Request нужно подготовить и проверить по
реальным build artifacts; вымышленные digests не подходят. Output должен быть
новым файлом: существующий файл или symlink блокирует запуск. `--cosign`
необязателен, значение по умолчанию — `cosign`.

## Контракт request

Вход — отдельный `MIGRATION_REHEARSAL_REQUEST`, version `1`, а не итоговый
migration manifest с уже существующим proof. Он содержит repository,
source/target release SHA и schema heads, target build run ID и attempt,
source/target backend/frontend images по immutable digest, frontend API URL,
ordered migration path с SHA-256 каждого script, canonical migration plan hash,
compatibility class и rollback policy. Неизвестные поля отклоняются.

Поддерживается `EXPAND_ONLY`; политики — `PRE_ACTIVATION_DOWNGRADE` и
`FAIL_FORWARD_ONLY`. Plan hash вычисляется функцией `migration_plan_hash` из
`scripts/release_manifest.py`. Команда проверяет принятие обоих SHA в `main` и
успешный CI, точный target build run/attempt, соответствие images его artifacts,
подписи и provenance всех четырёх images. Alembic heads сверяются точно;
revisions и hashes migration scripts извлекаются из target image без сети.

## Что проверяет rehearsal

Создаётся уникальный Compose project: PostgreSQL, Redis, backend и frontend,
внутренняя сеть, собственные volumes, без host ports и bind mounts. Workers и
schedulers отсутствуют; publishing, metrics и provider credentials выключены.
Readiness проверяется внутри контейнеров.

На source schema создаются активные и неактивный пользователи и связанные
синтетические campaign, task, content, approval, cancelled publication и audit
record. Обязательные business tables должны содержать строки. После encrypted
backup и отдельной проверки restore backend/frontend останавливаются.
Исходная fixture database удаляется и создаётся заново; ciphertext расшифровывается
потоком прямо в `pg_restore`, без plaintext dump на диске. До upgrade сверяются
восстановленная schema, counts и hash всех прежних business tables.

На восстановленной БД выполняются exact upgrade, `alembic check`, workspace и
membership invariants, target readiness и повторное сравнение данных. При
`PRE_ACTIVATION_DOWNGRADE` затем проверяются exact downgrade, удаление новых
workspace objects, source-image readiness и сохранность данных. Для
`FAIL_FORWARD_ONLY` downgrade не выполняется и соответствующее поле proof
остаётся отрицательным.

## Proof и границы проверки

Proof связывает release SHA, schemas, plan hash, target run/attempt и все image
digests. Он содержит checksum encrypted backup, исходные counts, hashes до
restore, после restore, upgrade и проверенного downgrade, результаты invariants
и readiness. Hash не зависит от порядка строк; payloads и credentials в proof
не включаются. PASS записывается только после успешного удаления временного
project, проверки отсутствия его containers/volumes/networks и удаления
временных backup/key files. Ошибка завершает команду без автоматического retry.

## Live CI

Обязательный job `release-tools` запускает этот migration rehearsal перед
существующим **schema-static** `release_drill.py`. Request закреплён в
`scripts/fixtures/migration-0028-to-0030.json`; он использует опубликованные
образы source build `44937076752` (`f0913e0029e25da58ea95504fd07b0989811dbb9`)
и target build `44954363133`, attempt `1`
(`dbd271eb61f14817147466128ed3b20a80fe8287`). Backend/frontend code target build
совпадает с `main@79a8104817ec609d79086f2bd2b2a038e2e9e4f5`; последующие
изменения main касались только manifest contract и его tests.

Digests взяты из artifacts этих builds. Migration hashes вычислены из точных
файлов target commit; runner повторно сверяет их с подписанным backend image.
Новые builds и `latest` tags не подменяют закреплённую пару автоматически.
При смене fixture или истечении срока хранения target build artifacts нужно
отдельно проверить и обновить request. CI не обходит недоступные artifacts.

После успешного прогона сохраняется отдельный artifact `migration-drill-proof`;
schema-static drill сохраняет собственный `release-drill-proof`. Логи показывают
безопасные этапы, без command arguments, credentials и business payloads.

Proof относится только к указанной паре images и синтетической fixture.
Оформление итогового manifest, integration с approval/apply и production
acceptance остаются отдельными этапами. Запуск этой команды не выполняет
deployment и не является разрешением на production migration.
