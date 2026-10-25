# Приёмка Этапа 2 — 2026-10-25

Статус: PASS для текущего localhost-размещения. Проверен полный release pipeline с отдельным человеческим согласованием и production digest deploy. Отключение GitHub environment admin bypass завершает документированное усиление approval boundary; оно не перезапускало приложение.

## Версии и доказательства

| Объект | Зафиксированный результат |
| --- | --- |
| Развёрнутый runtime | `9472225798064b4a8b0b971bf2589d67f35f7986` (PR55) |
| Исправленный операторский протокол | `abe07880615e4fa81ffe6f66e69c63f3c66fc84f` (PR56) |
| Предыдущий runtime | `51c21f18cebfc33db68b03ae3307c4c3c8a1053e` |
| Alembic | `20261009_0028`; DB downgrade не выполнялся |
| Post-merge CI протокола | [40236454287](https://github.com/afkochnev/iteam-ai-marketing/actions/runs/40236454287), точный `abe0788`, PASS |
| Сборки протокола | [40236837861](https://github.com/afkochnev/iteam-ai-marketing/actions/runs/40236837861), точный `abe0788`, PASS; не развёртывались |
| Подписанные образы runtime | [40229763721](https://github.com/afkochnev/iteam-ai-marketing/actions/runs/40229763721), точный `9472225`, PASS |
| Новый manual approval | [40236872474](https://github.com/afkochnev/iteam-ai-marketing/actions/runs/40236872474), reviewer `afkochnev`, workflow SHA `abe0788`, source `51c21f18` → target `9472225`, подписанный receipt PASS |
| Предыдущее использованное разрешение | `40230359806` осталось израсходованным; маркер не сбрасывался |

Точные принятые образы:

- Backend: `ghcr.io/afkochnev/iteam-ai-marketing-backend@sha256:7a59214435f1f62593444fb5204b98fa02e618f0de384bbaf605b9a9acc4c9a6`.
- Frontend: `ghcr.io/afkochnev/iteam-ai-marketing-frontend@sha256:343978b7a4237bf3d2b5865aff523b940a45d2b9b3839d035a178b0c3ffac0f8`.

Проверены обе архитектуры ARM64/AMD64, подписи с identity release workflow, OCI revision, SBOM и provenance. Manifest после deploy повторно совпал с signed receipt. BUILD_SHA устанавливался инструментом из проверенного manifest, без ручной подстановки release SHA и без production build.

## Критерии приёмки

| Критерий исходного аудита и операционного протокола | Доказательство | Итог |
| --- | --- | --- |
| CI/main → multi-arch GHCR → signatures → immutable digest | Указанные build runs и exact manifest | PASS |
| Ручное production environment approval | Реальная запись `afkochnev`, подписанный receipt и одноразовый marker | PASS |
| Проверка фактических GitHub gates | Main PR/checks/enforce-admins, protected branches и required reviewer; admin bypass выключен и повторно прочитан через API | PASS |
| Повторяемый release без компиляции на production | `apply` по digest, точные runtime BUILD_SHA/git HEAD `9472225` | PASS |
| Репетиция upgrade/rollback/injected failure | Artifact `release-drill-proof` в CI 40236454287: 11 сервисов, signed `51c21f18 → 9472225`, PASS; исходный код отказа сохранён | PASS |
| Свежий encrypted backup и isolated restore перед deploy | Backup 2026-10-25 21:49:35 UTC, число строк во всех 35 public tables совпало, restore network=none | PASS |
| Отдельно разрешённый production deploy | Операция `40236872474`, PASS 2026-10-25 21:55:09 UTC | PASS |
| Postflight | 11 сервисов, worker queues, health/readiness, `/publications` HTTP 200, Alembic и image labels | PASS |
| Откат без пересборки | Сохранены baseline и точные старые image IDs для всех 9 app-сервисов с rollback tags | PASS |
| Сохранность настроек и бизнес-состояния | Provider gates, credentials policy, публикации и аудит не изменились; тестовых публикаций 0 | PASS |

При первом переходе исходный runtime состоял из локально собранных образов. Для точного отката сохранены их immutable image IDs и baseline, а не подмена на другой GHCR build с тем же commit. Текущий runtime уже привязан к подписанным GHCR digest. Состояние операции хранится локально в `~/iTeam-private/release-operations/iteam-ai-marketing/`: `40236872474-before.json`, `40236872474-result.json`, `current-release.json`, rollback overlay и одноразовые markers. Секреты и ключи backup не входят в этот отчёт или CI artifacts.

## Состояние и границы

- `APP_ENV=production`, Secure-cookie включены; Chromium login/profile reload проверены, пароли пользователей не менялись.
- Publishing: VK=true, Telegram=true. Metrics: VK=true, Telegram=false.
- VK target `-150574411`; refreshable credentials остаются в изолированном volume, UID 10001, файл 0600. Метрики и publishing credentials разделены.
- GitHub environment `production`: `can_admins_bypass=false`; reviewer `afkochnev`, protected branches, self-review разрешён осознанно для единственного владельца. Это не независимое согласование двумя людьми. Отключение bypass не отменяет возможность владельца вручную подтвердить запрос.
- Временный CI-стенд использовал отдельные данные, выключенные providers, закрытую сеть и не получал production credentials; project/volumes удалены.
- В первоначальной неуспешной попытке выполнен проверенный откат. Исправление PR56 разделило встроенные defaults образа и Compose policy, сохранило строгую проверку фактического environment и добавило безопасное сохранение первого кода отказа. Успешный deploy выполнен по новому approval.
- Приёмка не распространяется на внешний домен, HTTPS, Safari, schema-changing deploy или доступность старых GitHub artifacts после retention. Смена frontend API URL требует новой сборки. При отсутствии records протокол закрывается ошибкой; mutable tag не используется как fallback.

Следующий шаг — отдельный Этап 3 observability/operations. Приоритет: ошибки, состояние очередей и heartbeat workers, overdue/failed publications и alerts с безопасной обработкой данных. Тестовая внешняя публикация для приёмки Этапа 2 не требовалась и не выполнялась.
