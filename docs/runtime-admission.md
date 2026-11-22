# Пассивный runtime и граница активации

Этот блок реализует запрет работы приложения в пассивном процессе и проверяет
необратимую границу активации на disposable candidate. Это часть этапа 5
[ADR 013](adr/013-production-schema-migration-releases.md).

Production activation adapter и новый signed approval для активации остаются
отдельной частью протокола. Существующие migration approval и pre-activation
apply сохраняют свои прежние полномочия. Candidate proof не принимается как
production approval или rehearsal подписанного release image.

## Режим выбирается при запуске процесса

`RUNTIME_ADMISSION_MODE` принимает только `active` или `passive`.
По умолчанию используется `active`, сохраняя поведение существующих процессов.
Неизвестное значение закрывает запуск; диагностическая ошибка не должна
включать переданное значение.

`APP_ENV=production` с явным `passive` сохраняет production validation.
Legacy `APP_ENV=maintenance` всегда даёт эффективный passive mode, в том числе
при ошибочно указанном `RUNTIME_ADMISSION_MODE=active`.

Ручная утилита `vk_credentials_admin` использует отдельный профиль `vk-admin`,
фиксированный credential CLI entrypoint и явные `APP_ENV=credential_admin`,
`RUNTIME_ADMISSION_MODE=active`. Это сохраняет её команды status/preflight/
provision/refresh; publishing flags остаются выключенными. Она не запускается
как часть обычного runtime. Исключений для credential refresh внутри passive
процесса нет.

Решение фиксируется один раз на время жизни процесса. Изменение `.env`,
`os.environ` или объекта settings не открывает уже запущенный passive process.
Переход выполняется через создание новых контейнеров с новой конфигурацией;
обычный restart не является сменой admission configuration.

Параметр режима сам по себе не удостоверяет разрешение оператора. Будущий
production controller обязан проверить отдельную authority, зафиксировать
необратимую границу и только затем создавать ACTIVE runtime. Текущий backend
не выдаёт это разрешение и не загружает его из старого журнала.

## Что разрешено в passive

Пассивное ASGI-приложение выбирается до импорта business router и Celery.
Доступны только `GET /health` и `GET /health/ready`. Остальные пути и методы,
включая GET бизнес-данных, закрыты с `503` и фиксированным кодом `RUNTIME_PASSIVE`.

Ограничение всех business routes необходимо: некоторые GET выполняют
reconciliation и могут обновлять runs, tasks и audit history. Одного запрета
POST/PATCH/DELETE недостаточно.

Health сообщает `admission_protocol=1`, эффективный режим, build identity и
`business_ready=false`. Liveness не считается готовностью к бизнес-операциям.
Passive readiness использует отдельное read-only подключение PostgreSQL,
проверяет фактический режим транзакции и schema, затем выполняет Redis PING.
Она сохраняет совместимые поля `status`, `database`, `redis`, `schema`;
значение `ok` появляется только после соответствующей проверки.

Readiness не создаёт consumer, не получает business messages и не выполняет
provider calls. Redis PING и служебные команды установки соединения учитываются
отдельно от работы с business queues.

Обычный Celery worker или beat в passive завершается до bootstrap брокера.
Пустой schedule, выключенные provider flags, отсутствие credentials или
проверка внутри уже полученной task не заменяют этот запрет. Producer, claim,
execution и provider boundaries дополнительно закрывают прямые вызовы, в том
числе ранее выданный publication execution token.

Business session factories не должны выдавать passive-коду полномочия записи.
Read-only параметры PostgreSQL используются как дополнительная защита
диагностического подключения; они не являются отдельной ролью БД и не
ограничивают администратора, имеющего прямые database credentials.

## Необратимая граница в candidate fixture

[`migration_activation_state.py`](../scripts/migration_activation_state.py)
работает только с `DISPOSABLE_CANDIDATE_ONLY`, приватным сгенерированным
каталогом и project namespace `iteam-admission-drill-*`. Binding содержит
operation ID, точные candidate commit/image identity и target schema.

| Фаза | Что уже подтверждено |
| --- | --- |
| `PREPARED` | Создан новый журнал, существующая операция не переиспользована |
| `PASSIVE_VERIFIED` | Записаны связанные с candidate результаты реальных passive checks |
| `ACTIVATION_REQUESTED` | Зафиксировано намерение; дальнейшая неопределённость трактуется консервативно |
| `ACTIVATION_STARTED` | Неизменяемый marker и журнал синхронизированы и прочитаны обратно; текущий объект может разрешить fixture activation |
| `ACTIVE` | Новый runtime создан после границы |
| `CONTAINMENT_REQUESTED` | Разрешение на дальнейшую активацию отозвано; физическое containment ещё требует проверки |
| `CONTAINED` | Подтверждены остановка runtime, блокировка новых claims и сохранение target schema/известной записи |
| `OPERATOR_RECOVERY_REQUIRED` | Полный безопасный результат либо достоверность записей подтвердить нельзя |

До первого reconnect или ACTIVE recreation выполняется `begin_activation()`.
Он сохраняет intent, эксклюзивно создаёт `.started` marker, выполняет `fsync`
файла и каталога, проверяет readback и затем сохраняет следующую фазу.
Неуспех любого шага запрещает выдачу разрешения. Существующие journal/marker
не заменяются новой операцией.

Разрешение принадлежит исходному объекту и процессу: наследование через `fork`
его не переносит. Проверки связывают bytes файлов с их inode и metadata;
замена marker или journal идентичной копией также прекращает разрешение.
Новая identity journal принимается только после собственной durable записи.

Нет load/resume, automatic retry, downgrade или restore API. Во всех отчётах
`schema_downgrade_allowed=false`; production approval, production activation
и внешние side effects остаются неразрешёнными. Результат чтения
[старого журнала](migration-operation-inspection.md) не восстанавливает
execution authority.

Ошибка записи containment intent не должна мешать физической остановке.
До фактического stop/fence нельзя утверждать, что новые claims закрыты:
уже работающий ACTIVE process продолжает жить со своей исходной конфигурацией.
При повреждении state положительные assertions о сохранении данных и
успешном containment снимаются.

## Что проверяет candidate CI

[`runtime_admission_drill.py`](../scripts/runtime_admission_drill.py) создаёт
только собственные disposable PostgreSQL/Redis/application resources.
CLI принимает output path, но не production root, database URL или namespace.

Candidate собирается из tracked backend context точного Git checkout.
Proof различает фактический checkout commit/tree, включая synthetic merge
commit PR, и immutable local image ID. В контейнерах проверяются реальные
identity и admission protocol. Локальному образу не приписываются подпись,
GHCR digest или BUILD_SHA другого release.

Passive fixture содержит действия, которые действительно могли бы изменить
данные: API mutation, GET с reconciliation, queued/stale runs, work для
dispatcher/recovery и сообщения брокера. Измеряется полный target snapshot,
включая workspace tables, и непрерывное наблюдение за broker activity.
Потеря наблюдения не подтверждает отсутствие claims. Допустимые health probes
не скрывают операции business queues.

Отдельный activation scenario подтверждает durable boundary до reconnect и
создания ACTIVE процессов, реальный commit через HTTP и настоящий worker claim.
Отказ внедряется после подтверждённой записи. Containment должен сохранить
target schema и новую запись, закрыть дальнейшую работу и не выполнить
downgrade, backup restore или включение source runtime.

Короткие unit tests проверяют write/fsync/readback failures и crash windows.
Отдельный процесс прекращает работу после синхронизации marker: новая
операция не получает права продолжить его выполнение. Это проверка process
crash, а не утверждение о поведении накопителя при потере питания.

Proof публикуется только после завершения проверок и удаления disposable
контейнеров, сетей, volumes, наблюдателей и временных секретов.

## Связь с production release protocol

Существующие signed-image migration/release drills продолжают выполняться.
Их pinned images появились до нового admission gate, поэтому они не служат
доказательством его работы. Candidate proof проверяет код и поведение
изолированного fixture, сохраняя отдельный тип authority.

Для завершения production-части этапа 5 необходимы новый activation-capable
approval envelope, проверенный signed gate-capable image, binding к точной
операции и target runtime, а также production adapter для activation и
post-activation containment. После публикации такого image требуется
отдельный signed end-to-end acceptance. Первая production migration остаётся
отдельно разрешаемым этапом 6.
