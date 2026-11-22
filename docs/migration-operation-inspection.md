# Чтение сохранённой migration operation

[`scripts/migration_operation_inspect.py`](../scripts/migration_operation_inspect.py)
проверяет согласованность уже записанных документов migration apply. Он полезен
для передачи операции другому оператору и разбора прерывания процесса.
Результат описывает **сохранённые записи**, а текущая граница writers всегда
остаётся `UNKNOWN`.

Это подготовительная часть этапа 5 [ADR 013](adr/013-production-schema-migration-releases.md).
Команда не реализует activation, resume или post-activation recovery.
Даже полностью согласованный журнал `PRE_ACTIVATION_READY` не даёт новому
процессу доказательство непрерывного наблюдения и разрешение downgrade.

## Вызов

Из checkout принятой версии инструмента, под тем же оператором, который
выполнял apply:

```bash
python3 scripts/migration_operation_inspect.py --operation-id "$OPERATION_ID"
```

`OPERATION_ID` — известный идентификатор существующей операции. Допускается
существующая grammar Journal: 1–64 символа, первый — строчная латинская буква
или цифра, далее также `_` и `-`. Обычно apply создаёт 32 hex-символа.
Команда не перечисляет другие операции и не выбирает «последнюю» автоматически.

Каталог фиксирован:
`~/iTeam-private/release-operations/iteam-ai-marketing` текущего оператора.
CLI принимает только `--operation-id` и стандартный `--help`. Флагов выбора
каталога, production root, resume, activation, downgrade или force нет.
Импортируемая функция `inspect_operation(state_dir, operation_id, *, now=None)`
позволяет тестам передать disposable directory и фиксированное время; CLI
такого обхода не предоставляет.

Результат печатается в stdout как один JSON object. `--help` выводит справку.
Exit code `0` означает `CONSISTENT_TERMINAL`; для остальных состояний — `1`.
**Exit code `0` не означает успешную миграцию или разрешение продолжать работу:**
согласованная запись `OPERATOR_RECOVERY_REQUIRED` тоже имеет этот код.

## Какие файлы читаются

| Документ | Проверка |
| --- | --- |
| `operation-<id>.json` | Закрытая schema, manifest/hash, approval metadata, последовательность действий, phases, pending action, ошибки и evidence |
| `approval-<run>.consumed` | Operation ID, manifest, receipt hash, run/artifact ID и время расходования совпадают с журналом |
| `migration-runtime-<id>.json` | Полная вложенная schema исходного inventory; canonical hash совпадает с initial evidence журнала |
| `migration-backup-<id>.json` | Структура proof и привязки к operation, manifest, database identity, source images/schema и backup evidence |

Чтение использует уже существующий `operation.lock` с shared nonblocking lock.
Если apply держит exclusive lock, инспектор возвращает `UNAVAILABLE`.
Каталог и lock не создаются; файловые дескрипторы открываются только для чтения.
Каталог должен принадлежать оператору и иметь режим `0700`, файлы — `0600`.
Symlink, hardlink, FIFO, чужой владелец и документ больше 4 MiB отклоняются.

Проверяются inode, размер, mode, owner, число ссылок, mtime и ctime до и после
чтения. Замена файла, даже на файл с теми же bytes, приводит к отказу.
JSON с duplicate keys, NaN/Infinity, неизвестными полями и неверными типами
не принимается. Чтение может изменить filesystem atime; содержимое, права
и mtime инструмент не меняет.

Ciphertext, backup key, `.env`, provider credentials и business rows не
читаются. Инспектор не вызывает Docker, SQL, GitHub, Cosign или production
entry point и не создаёт объект `Journal` для дальнейшей записи.

## Значения отчёта

| `record_state` | Смысл |
| --- | --- |
| `CONSISTENT_TERMINAL` | Доступные документы структурно согласованы с завершённым результатом журнала; результат может быть ошибкой |
| `INCOMPLETE` | Операция не имеет terminal record либо отсутствуют обязательные подтверждающие документы |
| `INVALID` | Обнаружены противоречие, небезопасный файл, некорректная schema/JSON или изменение snapshot во время чтения |
| `UNAVAILABLE` | Журнал/каталог недоступен, lock отсутствует/занят либо чтение не удалось |

`recorded_phase` и `recorded_status` содержат только проверенные значения
журнала. При `INVALID` и `UNAVAILABLE` они и обе оценки свежести очищаются
до `null`, включая отказ на последней проверке неизменности файлов.
`error_code` берётся из закрытого набора `MIGRATION_INSPECTION_*`; исходный
текст исключения, пути и содержимое повреждённых документов не выводятся.

Отсутствие и противоречие различаются. Например, consumed marker может быть
уже записан, когда журнал ещё содержит `VERIFIED`: marker записывается первым.
Аналогично runtime baseline и backup proof могут существовать при незавершённом
`begin backup`. Такие согласованные окна прерывания дают `INCOMPLETE`.
JSON `null` внутри существующего файла не считается отсутствующим документом.

История проверяется по существующему порядку engine: sequence, переходы phases,
попытки действий, completion/failure, recovery и terminal state. Снимок business
data при initial inventory может отличаться от snapshot последующего backup.
После backup evidence должно совпадать именно с backup baseline.

### Возраст документов и полномочия

`approval_time_eligible` и `backup_time_eligible` сообщают только о попадании
записанного timestamp в интервал от 0 до 3600 секунд относительно `observed_at`.
При отсутствии backup proof его поле равно `null`. Просроченный корректный
документ остаётся доступным для исторической инспекции; свежесть не превращает
его в новое разрешение. Проверка свежести при настоящем apply не изменена.

Во **всех** результатах, включая успешный exit code:

| Поле | Значение |
| --- | --- |
| `writer_boundary` | `UNKNOWN` |
| `live_runtime_checked` | `false` |
| `historical_continuity_verified` | `false` |
| `receipt_signature_verified` | `false` |
| `automatic_retry` | `false` |
| `activation_authorized` | `false` |
| `schema_downgrade_allowed` | `false` |

Локальная согласованность hashes не подтверждает подпись receipt, подлинность
документов из внешнего источника или прошлый успешный `fsync`. Исходный runtime
inventory не является текущим Docker inventory. После закрытия event monitor
новый процесс не восстанавливает пропущенную историю из журналов или позднего
успешного `inspect`.

## Проверка в CI

Unit tests используют настоящий `Journal` и engine с disposable fake runtime.
Проверяются успех, rollback, operator recovery, pending action, crash windows,
исторические timestamps, cross-bindings, redaction, запрет записи и подмена
файлов во время чтения.

Настоящий Docker [pre-activation drill](migration-preactivation-apply.md)
проверяет оба исхода на изолированных контейнерах. После проверки живой границы
он закрывает adapter/event monitor под exclusive lock, освобождает lock и
инспектирует сохранённые документы. До удаления fixture обязательны
`CONSISTENT_TERMINAL`, ожидаемый recorded status и все ограничения полномочий
из таблицы выше. Ошибка инспекции запрещает публикацию `PASS`, cleanup всё равно
выполняется.

Proof типа `MIGRATION_PREACTIVATION_DRILL` имеет `proof_contract_version=2` и
содержит `post_close_record_inspection` для обоих сценариев. Записанное ранее
`writer_boundary=PROVEN_CLOSED` относится к проверке внутри живого apply;
вложенный отчёт после закрытия monitor содержит `UNKNOWN`. Контракт отдельного
approval rehearsal не меняется. Fixture proof остаётся
`DISPOSABLE_FIXTURE_ONLY` с `production_approval_verified=false`.

## Следующая часть этапа 5

Следующий [candidate-блок](runtime-admission.md) добавляет пассивный старт
runtime с запретом business writes/claims, durable one-way marker до reconnect
и проверку containment после реальных HTTP/worker записей. Его evidence имеет
отдельную authority `DISPOSABLE_CANDIDATE_ONLY` и не расширяет полномочия
этого инспектора или прежнего migration approval.

В старых pinned release images `APP_ENV=maintenance` сам по себе не закрывает
business writes и Celery consumers; read-only engine readiness helper действует
только внутри этого helper. Новый backend с `admission_protocol=1` принудительно
выбирает passive mode для maintenance. Свойства нового candidate нельзя
приписывать старому подписанному образу по одному имени environment.

Для завершения этапа 5 остаются activation-capable signed approval, production
activation/containment adapter и signed end-to-end acceptance на образе с новым
gate. Первая production migration остаётся отдельно разрешаемым этапом.
