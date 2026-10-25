# Этап 2: подтверждение, digest deploy и откат

## Граница доверия

GitHub Actions собирает и подписывает образы, проверяет сценарий обновления/отката и после ручного подтверждения подписывает точный release manifest. GitHub Actions не получает SSH-доступа, паролей БД, publishing credentials или production runner. Локальный оператор отдельно выполняет развёртывание, используя проверенный manifest.

`prepare` не является одобрением и не меняет production. `apply` не принимает произвольный image, тег, локальный manifest или флаг обхода approval. Он загружает подписанный receipt из успешного `Approve release` run и проверяет реальную историю ручного approval, автора `afkochnev`, main ancestry, CI, подпись, digest, SBOM/provenance и ожидаемый исходный BUILD_SHA. Одобрение действует час и расходуется до первой попытки переключения; после неуспешной попытки нужен новый approval run. Одобрение нельзя сбросить командой этого инструмента.

## Настройки GitHub — отдельное применение и проверка

Подготовлены `infra/release/main-protection.json` и `production-environment.json`. Они не применяются автоматически из workflow или при merge PR.

После согласования настроек оператор применяет их через GitHub API:

```bash
gh api --method PUT repos/afkochnev/iteam-ai-marketing/branches/main/protection --input infra/release/main-protection.json
gh api --method PUT repos/afkochnev/iteam-ai-marketing/environments/production --input infra/release/production-environment.json
python3 scripts/release_ops.py check-gates
```

Main требует PR, зелёные `backend`, `frontend`, `release-tools`, актуальность относительно main, разрешение обсуждений; force push и удаление запрещены, проверки распространяются на администратора. Число обязательных PR-review равно нулю: единственный владелец не может утверждать собственный PR как независимый reviewer. Принятие PR человеком по рабочему процессу проекта сохраняется.

Production environment требует отдельного ручного подтверждения владельцем `afkochnev` (GitHub ID 186186368). Self-review разрешён осознанно, чтобы единственный владелец мог подтвердить инициированный им workflow; это один человек, не правило двух независимых reviewers. Доступ другим людям и командам не добавляется. Дополнительно в UI GitHub следует отключить admin bypass. Локальный `apply` в любом случае требует запись фактического approval, а не только успешный workflow или название environment.

Workflow использует минимальный GitHub token для чтения публичных release records/образов и OIDC для подписи. Проверку административных правил выполняет локальный `check-gates`/`apply` с доступом оператора, а не расширенный token в CI.

## Подготовка и подтверждение

1. После успешного main CI дождаться `Release images` для точного commit.
2. Подготовить manifest: `python3 scripts/release_ops.py prepare --build-run BUILD_RUN_ID --output /private/tmp/release.json`. Нужны `gh`, Docker Buildx и официальный `cosign` в PATH (или глобальный `--cosign /path/to/cosign`). Образы не запускаются, production не меняется.
3. Запустить `Approve release` на main. Указать точный `build_run_id` и `expected_current_release_sha`, сверенный с production BUILD_SHA.
4. Человек открывает `release-for-review`, сверяет исходный SHA, целевой SHA и оба digest, затем вручную подтверждает production job в GitHub. Агент не нажимает approval от имени владельца. После подтверждения workflow подписывает receipt, включающий ID approval run, его attempt, SHA workflow, исходный SHA и manifest.
5. Rollback к прежнему принятому release проходит тот же workflow с прежним build run ID и текущим исходным BUILD_SHA. Это новое явное решение человека.

## Backup и применение на локальном production-хосте

Пример для текущего размещения проекта; папки backup/ключей должны находиться вне checkout, принадлежать оператору и иметь режим 0700:

```bash
python3 scripts/release_backup.py --root /Users/alexanderkochnev/iteam-ai-marketing --backup-dir /Users/alexanderkochnev/iteam-backups --key-dir /Users/alexanderkochnev/iTeam-private/backup-keys --proof /private/tmp/release-backup-proof.json
python3 scripts/release_ops.py apply --root /Users/alexanderkochnev/iteam-ai-marketing --approval-run APPROVAL_RUN_ID --backup-proof /private/tmp/release-backup-proof.json
```

Backup использует единый repeatable-read snapshot: pg_dump сразу шифруется AES-256-CBC/PBKDF2 (200000 итераций), открытый dump на диск не попадает. Отдельный случайный ключ сохраняется с правами 0600. Восстановление идёт в новый контейнер с `network=none`, без портов; сравниваются количества строк всех public tables с тем же snapshot. Контейнер и его данные удаляются. Proof связан с исходным release, checksum файла, успешным восстановлением и сроком один час.

`apply` блокирует параллельные операции. Состояние и расходованные approval ID хранятся в `~/iTeam-private/release-operations/iteam-ai-marketing` (0700, файлы 0600). Выбор другого state-dir через CLI отсутствует. Receipt с другим исходным BUILD_SHA отклоняется до изменения runtime.

До остановки сервисов проверяются отсутствие PUBLISHING, просроченных/близких (30 минут) SCHEDULED и RUNNING AgentRuns, canary gate, схема, текущие image revisions, credentials owner/mode и отсутствие static VK publishing token. Проверяются команды, переменные, пользователи, порты, сети, resource/security policies и mounts. VK volume допустим только у publication worker и credentials admin, включая случаи другого target mount path.

Образы скачиваются только по digest. Сначала проверяются их OCI revision, backend BUILD_SHA и Alembic head. Текущий первый протокол разрешает только неизменную схему `20261009_0028` и неизменный docker-compose.yml; изменение миграций или topology требует отдельной доработки/проверки. Автоматического downgrade нет. Старые работающие образы закрепляются локальными immutable image IDs для точного аварийного возврата, включая первый переход с прежней схемы локальных сборок. Это не пересборка старой версии.

Порядок переключения: stop schedulers → drain workers → повторный quiet gate → новый backend/schema check/readiness → frontend/HTTP readiness → workers/проверка каждой очереди по hostname нового контейнера. Используется только `up --no-build`. В `.env` меняются только BUILD_SHA и COMPOSE_FILE; исходные overrides сохраняются, digest overlay находится в приватном state-dir. Production checkout переводится на точный принятый main commit. Изолированный VK volume не заменяется.

После запуска проверяются BUILD_SHA, все 11 сервисов, прежние команды/параметры/volumes, provider gates, credentials и хеши записей публикаций/их аудита. При ошибке останавливаются новые workers и возвращаются точные старые образы и SHA. БД не восстанавливается и бизнес-состояния не переписываются. Если recovery не подтверждён, инструмент возвращает `ROLLBACK_REQUIRES_OPERATOR_ATTENTION`. Слепой повтор и автоматическое расходование нового approval отсутствуют.

## Репетиция

`release-tools` CI выбирает два последних успешных build run с разными main SHA (ID включаются в proof), запускает unit/failure tests и настоящий disposable Compose-проект: предыдущие подписанные образы → следующие подписанные образы → возврат → искусственный сбой запуска frontend → автоматический возврат.

В проекте 11 сервисов, отдельные volumes и internal network, нет опубликованных портов, bind mounts, production credentials и доступа к внешней сети. Publishing/metrics выключены, OpenAI key пуст. Через Alembic создаётся отдельная тестовая БД, штатный seed создаёт непустые fixture-данные. Проверяются encrypted backup/isolated restore, неизменность fixture-данных, schema, SHA, topology, отключённых gates и HTTP frontend после rollback. Project/volumes удаляются; в artifact уходит только metadata proof, не backup или ключ.

Для ручного запуска требуется отдельный Docker runner с минимум 6 GiB памяти. На текущем Docker Desktop (4 GiB, обслуживает production) полная репетиция запрещена самим скриптом. CI использует отдельный runner, с ограничениями CPU/memory и solo Celery workers.

## Приёмка Этапа 2

Не считать Этап 2 завершённым, пока не выполнены все пункты:

- PR с этим протоколом принят, точный post-merge CI и Release images зелёные.
- Реальная репетиция upgrade/rollback/injected failure завершена PASS на final head.
- Настройки main/environment применены и `check-gates` прошёл.
- Реальный manual approval создал проверяемый signed receipt для конкретных source/target SHA.
- Выполнены свежие encrypted backup/isolated restore и отдельно разрешённый production digest deploy; postflight PASS и предыдущий manifest сохранён.

Документация GitHub: [environment protection](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments), [review history API](https://docs.github.com/en/rest/actions/workflow-runs#get-the-review-history-for-a-workflow-run). Настройки, CI proof и production-приёмка являются разными проверками.

Release records в GitHub Actions подчиняются retention репозитория. При истёкших/отсутствующих artifacts проверка закрывается ошибкой; переход на mutable SHA-tag в качестве обхода запрещён. Для архивного release требуется восстановить проверяемые records либо отдельно пересмотреть протокол хранения. Локальные предыдущие manifests и pinned image IDs сохраняются независимо от CI artifacts.
