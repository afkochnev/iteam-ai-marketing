# Migration apply до активации writers

`scripts/migration_release_apply.py` реализует **этап 4**
[ADR 013](adr/013-production-schema-migration-releases.md): применение
подтверждённой миграции до закрытой границы активации. Первый поддерживаемый
переход — `20261009_0028 → 20261121_0029 → 20261121_0030`.

Успех `PRE_ACTIVATION_READY` означает, что target schema, сохранность прежних
данных и maintenance-readiness проверены. **Все девять исходных application
containers остаются остановленными. PostgreSQL работает без сетевых
подключений Docker; у PostgreSQL и application containers установлен
`restart=no`.** Эти условия сохраняются и при подтверждённом возврате
`ROLLED_BACK_PRE_ACTIVATION` на source schema.

Активация приложений, reconnect PostgreSQL, post-activation recovery и первая
production-приёмка — отдельные этапы ADR. Merge инструмента и успешный CI
не являются разрешением production migration.

## Предварительные условия

Нужен новый receipt из **Approve migration release**, подтверждённый вручную
через environment `production`. Проверяются exact workflow run и artifact,
история решения, branch/environment protections, Sigstore identity,
source/target images, migration plan и связанный rehearsal proof. Receipt
schema-static workflow **Approve release** не подходит. Полный порядок
подготовки описан в [migration-release-approval.md](migration-release-approval.md).

Инструмент запускается из отдельного checkout принятой версии.
`--root` указывает на **чистый production checkout exact `source_release_sha`**.
Его `HEAD`, `.env` с source `BUILD_SHA`, OCI revisions и immutable image IDs
приложений должны согласовываться с manifest.

До расходования approval выполняются read-only inventory и quiet gate:

- Compose project — `iteam-ai-marketing`; все 11 ожидаемых сервисов имеют
  однозначные контейнеры и работают без pause/restart/OOM. Все четыре exact
  image references — source/target backend и frontend — уже находятся в
  локальном Docker cache под подтверждёнными digest: inventory использует
  `docker image inspect`, helpers — `--pull=never`. Runtime не скачивает и
  не пересобирает images; проверка approval через GitHub/Cosign этого не заменяет.
- `.env` принадлежит текущему оператору, имеет режим `0600`, не является
  symlink; `APP_ENV` равен `production` или `prod`. Production root —
  абсолютный путь. Разрешены корневой `docker-compose.yml` и overlays из
  canonical state directory. Непустые `COMPOSE_*` в окружении процесса
  запрещены; разрешённый `COMPOSE_FILE` читается из production `.env`.
- Source backend, workers и schedulers используют одинаковые database/Redis
  endpoints и source `BUILD_SHA`. `VK_PUBLISHING_ENABLED`,
  `TELEGRAM_PUBLISHING_ENABLED`, `METRICS_SYNC_ENABLED`, `VK_METRICS_ENABLED`
  и `TELEGRAM_METRICS_ENABLED` должны быть `false`. Отсутствующее значение
  gate трактуется как `false` согласно defaults. Apply не включает providers;
  временные maintenance helpers также отключают эти gates и не получают
  provider credentials.
- Нет `QUEUED`/`RUNNING` AgentRuns, `QUEUED`/`PUBLISHING` publications или
  `SCHEDULED` publications без времени либо со временем не позже ближайших
  30 минут. Все проверяемые Celery queues, их priority variants и Redis
  unacked structures пусты. Quiet gate повторяется после drain.
- PostgreSQL использует local user-defined bridge networks и **ровно один
  mount**: named volume в `/var/lib/postgresql/data`, с
  `PGDATA=/var/lib/postgresql/data`. Дополнительные mounts запрещены.
  `unix_socket_directories` должен быть одним из exact значений
  `/var/run/postgresql`, `/run/postgresql` или `/tmp`; эти directories остаются
  внутри container filesystem. Другие пути, в том числе socket в data volume,
  и список нескольких путей не поддерживаются. Так исключается доступ к БД
  через общий Unix socket в обход network fence.
- Допустимо только extension `plpgsql`; subscriptions и prepared transactions
  отсутствуют. `shared_preload_libraries`, `session_preload_libraries` и
  `local_preload_libraries` пусты. Неизвестные sharers PostgreSQL network
  namespace блокируют операцию.

На время операции требуется **исключительное использование тихого Docker
host**: без параллельного deploy, автоматического рестарта других контейнеров,
изменения topology и прямых административных записей в БД. Общий lock
сериализует release tooling, но не ограничивает другого администратора Docker.
После фиксации границы запуск любого контейнера, не зарегистрированного как
helper операции, делает доказательство непрерывности недействительным.

## CLI и расположение файлов

Production entry point —
[`scripts/migration_release_apply.py`](../scripts/migration_release_apply.py).
Он сам создаёт и проверяет свежую резервную копию; `--backup-proof` не принимает.

| Параметр | Назначение |
| --- | --- |
| `--root` | Абсолютный путь к production checkout на source SHA |
| `--approval-run` | ID успешного первого run **Approve migration release** |
| `--approval-artifact` | Exact artifact ID `approved-migration-release` из этого run |
| `--backup-dir` | Отдельный private directory для encrypted backup |
| `--key-dir` | Другой private directory для backup key |
| `--cosign` | Проверяемый Cosign executable; по умолчанию `cosign` |

В [release-operations.md](release-operations.md) для текущего размещения
зафиксированы production root `/Users/alexanderkochnev/iteam-ai-marketing`,
backup directory `/Users/alexanderkochnev/iteam-backups` и key directory
`/Users/alexanderkochnev/iTeam-private/backup-keys`. Перед отдельной
production-операцией эти пути сверяются с фактическим размещением.

State directory всегда равен
`~/iTeam-private/release-operations/iteam-ai-marketing` текущего оператора;
параметра `--state-dir` нет. State, backup и keys находятся вне production
checkout. Directories принадлежат оператору и имеют режим `0700`, создаваемые
файлы — `0600`. Backup и key directories различаются и не вложены друг в
друга; symlink и повторное использование существующих output files
отклоняются.

Для просмотра интерфейса без выполнения операции:

```bash
python3 scripts/migration_release_apply.py --help
```

Рабочему вызову нужны Git, Python, GitHub CLI с доступом к release/protection
records, Docker с Compose/Buildx, Cosign и OpenSSL. Запуск apply требует
отдельного разрешения production-операции.

## Порядок и доказательства каждого этапа

После успешной проверки receipt, inventory и quiet gate создаётся journal
`VERIFIED` с manifest и исходными identity hashes. Затем approval расходуется
до первой попытки backup и изменения runtime.

| Завершённая фаза | Проверяемый результат |
| --- | --- |
| `APPROVAL_CONSUMED` | Durable marker связывает approval run, artifact, receipt hash, manifest hash и operation ID |
| `BACKUP_VERIFIED` | Encrypted backup восстановлен в isolated clone; source/restore business hashes и row counts совпадают; clone удалён |
| `SCHEDULERS_STOPPED` | Restart отключён у apps и PostgreSQL; schedulers, frontend и backend остановлены |
| `WORKERS_DRAINED` | Все ожидаемые worker hostnames ответили; active/reserved/scheduled tasks исчерпаны; повторный quiet gate пройден |
| `WRITERS_STOPPED` | Все application containers подтвердили завершённый warm shutdown |
| `DATABASE_FENCED` | PostgreSQL отключён от всех Docker networks; проверены namespace, закрытие прежних host endpoints и непрерывность наблюдения |
| `SOURCE_VERIFIED` | Exact source schema, source Alembic check/readiness, исходная database identity и business state из backup подтверждены после остановки writers |
| `TARGET_PREFLIGHT_VERIFIED` | Target image имеет exact head и проверенные ordered migration scripts с ожидаемыми hashes |
| `UPGRADE_COMPLETED` | Выполнен `alembic upgrade` exact target revision; target schema и прежний business state проверены внутри закрытой границы |
| `TARGET_VERIFIED` | Прошли `alembic check` и migration-specific workspace/membership invariants |
| `MAINTENANCE_READY` | Target backend/frontend проверены только во временном изолированном runtime; helpers удалены и граница проверена повторно |
| `PRE_ACTIVATION_READY` | Операция завершена; target schema готова к отдельному решению об активации |

Workers останавливаются через warm shutdown; истечение deadline
не превращается в принудительный `SIGKILL` или успешный drain.

## Backup связывает source, restore и остановленную БД

[`migration_apply_backup.py`](../scripts/migration_apply_backup.py) открывает
в exact source backend image read-only транзакцию `REPEATABLE READ`,
экспортирует snapshot и вычисляет schema, counts и canonical business hash.
`pg_dump` использует **тот же exported snapshot**, пока транзакция открыта;
hash и dump относятся к одной версии данных.

Dump сразу шифруется AES-256-CBC/PBKDF2 с 200000 iterations без plaintext
на диске; случайный ключ хранится отдельно. Restore выполняется в новый
PostgreSQL container на том же immutable image с `network=none`, без ports.
Source backend image проверяет в clone exact source schema, counts всех
public tables и business hash.

Business hash охватывает исходные public tables, исключая `alembic_version`
и создаваемые этим переходом `workspaces`/`workspace_memberships`. Для новых
таблиц проверяются отдельные invariants ADR 013: единственный canonical
workspace `iTeam`, memberships активных пользователей, правильное отображение
ролей и отсутствие memberships неактивных пользователей.

Proof имеет закрытый набор полей и связывает operation ID, manifest hash,
production database identity hash, source release/schema/backend digest,
checksum ciphertext, одинаковые source/restore business states и cleanup.
Database identity учитывает PostgreSQL system identifier, database OID и имя
БД; identity clone закономерно отличается. Proof публикуется после удаления
restore containers и их данных; возраст при принятии — не более часа.

Резервная копия создаётся **до остановки writers**. Поэтому после stop/drain
и network fence apply заново читает исходную БД и требует полного совпадения
её business hash и counts с backup. Изменение между первоначальным
read-only inventory и snapshot backup само по себе допустимо; baseline
становится snapshot backup. Изменение после него блокирует upgrade;
baseline не заменяется новым hash или вторым backup.

Это же сравнение exact списка прежних tables, hash, counts и database
identity повторяется после upgrade, checks/readiness и при допустимом
восстановлении. Raw business rows, database URL, provider tokens и backup key
не попадают в proof или journal.

## Закрытая граница writers и maintenance-readiness

[`migration_writer_barrier.py`](../scripts/migration_writer_barrier.py)
сохраняет container/image IDs, restart policies и network attachments,
устанавливает `restart=no`, подтверждает остановку apps и отключает PostgreSQL
от всех networks. Одного пустого `pg_stat_activity` для этой границы недостаточно.

Проверки требуют неизменных container IDs/start timestamps/restart counts,
пустого списка PostgreSQL endpoints, только loopback interface и отсутствия
внешних routes. Прежние host endpoints должны перестать принимать подключения.
Runtime проверяет database identity и отсутствие client sessions, кроме
собственного probe и известного локального `pg_isready` healthcheck.

Docker event monitor работает в течение операции и синхронизируется
контрольными событиями. Он обнаруживает reconnect, restart, замену или запуск
контейнеров, способный нарушить границу. Поздний успешный `inspect` не заменяет
потерянный event stream: текущая остановка не доказывает отсутствие прошлых
записей. При выходе процесса monitor закрывается; изоляция и `restart=no`
сохраняются, но прежнее доказательство не разрешает resume.

Migration, snapshot и readiness helpers присоединяются только к изолированному
network namespace PostgreSQL (`container:<postgres-id>`) и обращаются к
loopback. Они не публикуют ports, не подключают host bind mounts и не получают
внешнюю сеть. Readiness использует временный Redis без persistence, exact
подписанные backend/frontend images и `APP_ENV=maintenance`.

Backend readiness запускается с отдельным SQLAlchemy engine, у которого
`default_transaction_read_only=on`; настройка проверяется до импорта
application routers. Проверяются `/health/ready`, database/schema/Redis status,
exact `BUILD_SHA` и HTTP frontend. Provider credentials пусты, gates выключены,
workers/schedulers не запускаются. Helpers удаляются до завершения фазы.
Это maintenance-readiness; production postflight остаётся отдельным этапом.

## Durable journal и одноразовое approval

[`migration_apply_state.py`](../scripts/migration_apply_state.py) использует
общий со schema-static operations `operation.lock` через
`release_ops.locked_state`, от проверки receipt до закрытия runtime adapter.

| Файл в state directory | Содержимое |
| --- | --- |
| `operation-<operation-id>.json` | Manifest, approval metadata, sequence, timestamps, phases, pending action, безопасные evidence и error codes |
| `approval-<approval-run>.consumed` | Одноразовое расходование в общей со static apply namespace; привязка к operation/manifest/receipt/artifact |
| `migration-runtime-<operation-id>.json` | Ограниченный исходный inventory контейнеров, сетей и restart policies; hashes Compose/environment и database identity |
| `migration-backup-<operation-id>.json` | Проверенный backup/restore proof без ключа и business payload |

Initial journal сохраняет source image IDs, database identity hash и hash
runtime baseline до расходования approval. Runtime baseline сохраняется
приватно до stop/drain. Файлы не содержат raw Docker inspect или environment.

При `consume()` свежесть approval проверяется повторно: timezone-aware timestamp
возрастом от нуля до 3600 секунд. Позднее истечение часа не прерывает recovery.

Journal и consumed marker создаются эксклюзивно с защитой от symlink и
перезаписи. Запись файла и directory entry подтверждается `fsync`.
**До каждого действия** journal сохраняет `pending_action`; завершение
записывается только после действия и его проверок. Ошибка записи, включая
ошибку directory `fsync` после rename, делает дальнейший journal недостоверным
для schema recovery. В таком случае разрешено только best-effort containment.

Load/resume API отсутствует. Production entry point блокирует новую операцию
при наличии **любого `operation-*.json`**, включая созданный до неудачного
consume. Новый approval не обходит блокировку; marker не переиспользуется.
Удаление/переименование state files, смена пользователя или обход через другой
инструмент не являются процедурой recovery.

Сохранённые документы можно проверить отдельной read-only командой
[`migration_operation_inspect.py`](migration-operation-inspection.md).
Она не создаёт Journal для исполнения, не проверяет живой runtime и всегда
возвращает `writer_boundary=UNKNOWN`, даже для согласованного terminal record.
Её успешный exit code не разрешает resume, activation или downgrade.

## Допустимый возврат на source schema

После ошибки engine удаляет свои helpers и пытается удержать apps остановленными,
а PostgreSQL изолированным. Recovery возможно только **в том же процессе**,
при исправном journal и непрерывном наблюдении.

Для exact target → exact source downgrade одновременно нужны:

1. До ошибки уже подтверждён `DATABASE_FENCED`; граница остаётся непрерывной,
   и containment её повторно подтвердил.
2. Database identity, hash и counts прежних business tables совпадают с
   подтверждённым backup baseline.
3. Текущая schema точно равна manifest `target_schema_head`. Intermediate
   revision `20261121_0029`, неизвестная schema или неоднозначное состояние
   writers не разрешают downgrade.
4. Manifest разрешает `PRE_ACTIVATION_DOWNGRADE`, а доверенный rehearsal
   доказал этот exact downgrade. `FAIL_FORWARD_ONLY` запрещает downgrade
   target schema.
5. Перед downgrade заново проходит target image migration preflight; затем
   target image выполняет downgrade до **exact source revision**.
6. После него подтверждены source schema, `alembic check`, source
   maintenance-readiness, исходные business data и image/configuration
   identity.

Если ошибка произошла до upgrade и текущая schema всё ещё exact source,
engine может подтвердить source без downgrade при тех же доказанных границе,
identity и неизменных данных, в том числе с policy `FAIL_FORWARD_ONLY`.

`restore_source_configuration` подтверждает исходные image IDs и неизменность
checkout/`.env`, которые не заменялись, и сохраняет остановку/изоляцию.
Результат — `ROLLED_BACK_PRE_ACTIVATION`; исходная ошибка остаётся в результате,
CLI завершается с ненулевым exit code.

**Автоматического restore backup поверх живой БД нет ни в одной ветке.**
Backup сохраняется для отдельно рассматриваемого operator recovery.
После возможной активации writers автоматический schema downgrade запрещён.

## Результаты и действия при прерывании

| Результат | Значение и дальнейшее действие |
| --- | --- |
| `PRE_ACTIVATION_READY` | Target schema проверена; граница `PROVEN_CLOSED`. Сохранить остановку/изоляцию и передать evidence для отдельного этапа активации |
| `ROLLED_BACK_PRE_ACTIVATION` | Source schema и данные подтверждены; граница `PROVEN_CLOSED`. Операция миграции не завершилась успешно; обслуживание по-прежнему не активировано |
| `OPERATOR_RECOVERY_REQUIRED` | Безопасный автоматический исход доказать нельзя; CLI указывает `writer_boundary=UNKNOWN`, исходный и отдельный recovery error code |
| `FAIL` до начала или вне управляемого recovery | Не делать вывод об отсутствии изменений только по exit code; сверить journal, consumed marker и реальное состояние |

Для всех результатов `automatic_retry=false`. При `OPERATOR_RECOVERY_REQUIRED`,
потере процесса, reboot, незавершённом `pending_action` или ошибке durable
state оператор сохраняет остановку/изоляцию, journal, marker, runtime baseline,
backup proof, ciphertext и отдельный ключ. Сначала read-only сверяются exact
schema, database/container/image identities, hashes и выполненные действия;
пробел в истории не трактуется как разрешение downgrade.

Дальнейший план принимается отдельно. `alembic stamp`, ручное изменение business
rows или live restore для получения ожидаемого статуса запрещены. Этап 4
не имеет команды разблокировки, очистки journal или возобновления.

Production ciphertext `migration-<operation-id>.dump.enc` и соответствующий
`migration-<operation-id>.key` **сохраняются** после успеха и ошибки. Их не
публикуют в CI artifacts, логах или Git. Наличие файлов после сбоя ещё не
означает завершённый backup: нужен полный proof. Ключ хранится отдельно до
решения о retention/recovery; его удаление делает backup невосстановимым.

## Что доказывает disposable CI

[`migration_apply_drill.py`](../scripts/migration_apply_drill.py) вызывает тот
же engine, Journal, backup и Docker adapter на disposable fixtures: успешный
`PRE_ACTIVATION_READY` и ошибка после target readiness с
`ROLLED_BACK_PRE_ACTIVATION`. Проверяются business hashes/counts, schema,
отсутствие активации, `restart=no`, пустые PostgreSQL network attachments
и недоступность прежнего тестового host port.

Затем adapter и event monitor закрываются под operation lock. После освобождения
lock инспектор читает настоящие сохранённые записи и требует согласованного
terminal outcome с `writer_boundary=UNKNOWN` и всеми execution-authority flags
равными `false`. В proof типа `MIGRATION_PREACTIVATION_DRILL` версии 2 это
отражено отдельным `post_close_record_inspection` для обоих сценариев.
Ошибка этой проверки запрещает `PASS`; fixture cleanup выполняется и при ней.

Application containers и Redis находятся только на internal bridge. Исходный
disposable PostgreSQL дополнительно подключён к собственной обычной bridge
сети `host_publish`, чтобы Docker действительно опубликовал ephemeral port
на `127.0.0.1`. На исключительно internal сети Docker сохраняет запрос в
`HostConfig.PortBindings`, но не создаёт доступный host port; это
[подтверждено maintainer Moby](https://github.com/moby/moby/discussions/53256).
До fence эта дополнительная сеть допускает outbound для тестового PostgreSQL;
provider credentials в нём нет. Proof поэтому отдельно фиксирует
`application_external_network=false` и
`postgres_has_host_publish_network=true`. Обе сети PostgreSQL отключаются
до migration и target helpers; `target_maintenance_external_network=false`
подтверждается теми же проверками namespace и непрерывности. Это проверяет
реальный published-port fence, не изменяя отдельный approval rehearsal.

Fixture manifest/approval IDs синтетические и явно маркированы
`authorization=DISPOSABLE_FIXTURE_ONLY` и
`production_approval_verified=false`. Drill не вызывает production entry
point. Images/provenance проверяются, но fixture proof не удостоверяет
актуальную production БД или согласие оператора на её изменение.

После CI удаляются только собственные disposable projects, volumes, helper
containers, backup и keys; остаётся безопасный proof. Drill требует отдельного
Docker runner с минимум 6 GiB памяти; запуск на production host запрещён.

Unit/failure-injection tests отдельно проверяют отказ каждой фазы,
несовпадение backup/quiesced data, потерю barrier continuity, неизвестную
schema, `FAIL_FORWARD_ONLY`, ошибку downgrade/source checks, KeyboardInterrupt,
сбой journal после mutation, запрет replay с новым approval и отсутствие
секретов в ошибках. Поведение сети и контейнеров проверяет настоящий Docker drill.
