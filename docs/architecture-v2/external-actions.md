# External actions

Статус: **Target architecture / Proposed**. Этот документ описывает правила будущего исполнения, не разрешает запуск providers или отправку каких-либо материалов. [ADR 004](../adr/004-intent-attempt-result.md), [ADR 005](../adr/005-transactional-outbox.md).

## Граница полномочий

**AI proposes → app validates → human approves → app executes.** Модель не получает send/publish tools и credentials. Её output — структурированное предложение. Приложение проверяет version provenance, допустимый action kind, target, права, policy, расходы и schedule; человек одобряет точный scope действия; отдельный исполнитель выполняет утверждённый intent.

| Действие | Субъект согласования | Доказательство результата |
| --- | --- | --- |
| CMS create draft | Версия статьи/иллюстрации, CMS target и draft action | Подтверждённый ID черновика; не публичная публикация |
| CMS publish/update | Точная версия, SEO/creative fields, canonical target и public/update action | ID, точный target и подтверждённый publication state; URL при наличии |
| Email campaign send | Версия письма, sender, audience snapshot, consent/suppression rules и send policy | Provider campaign/batch ID и уровень acceptance; дальнейшие delivery/bounce/unsubscribe — отдельные Observations |
| VK / Telegram post | Версия текста/creative asset, exact community/chat, время и разрешение отправки | Provider object/message ID, соответствующий target; URL, если провайдер её предоставляет |
| Сбор SEO/competitor/analytics | Разрешённый источник, scope, бюджет и privacy/access policy | Датированный Observation с источником и quality; не разрешение менять внешний объект |
| Запуск эксперимента | Гипотеза, variants, аудитория, окно, KPI, budget и stop rules | Принятая конфигурация; внешние эффекты идут через отдельные intents |

Approval связывается с hash/version параметров. Новый текст, другой target/audience, существенный creative change или новый вид действия требуют нового согласования. Перенос времени сохраняет одобренную версию и создаёт новую IntentRevision; применимость прежнего execution approval к переносу определяется явной policy, а не молчаливым выбором текущего текста. Точные правила допуска переноса — открытый вопрос Q4.

## Последовательность исполнения

1. Прикладная команда с actor, command idempotency key и expected revision блокирует/проверяет текущее состояние в PostgreSQL.
2. В одной транзакции записываются IntentRevision, ссылки на действительные approvals, audit и OutboxEvent. Без commit внешнего действия нет.
3. Outbox relay доставляет event ID в очередь с повторами. Потеря broker не теряет намерение. Дубли не создают второе действие.
4. Scheduler/worker сверяет due UTC instant, текущую revision, отмену/паузы, действующее разрешение и provider target. Future intents не исполняются раньше времени.
5. Worker атомарно получает ownership token/lease и сохраняет DeliveryAttempt до сети. Credential executor разрешает только scoped action/target, затем вызывает provider.
6. Подтверждение валидируется и в PostgreSQL сохраняются outcome, provider identity, PlacementRevision/ExternalPlacement и audit. Queue acknowledgement не заменяет этот commit.
7. Серверная проекция обновляет timeline; метрики собираются отдельно и не меняют историческое подтверждение доставки.

## Неоднозначность и повторы

DB-транзакция и удалённый HTTP-вызов не атомарны. Timeout, разрыв во время/после отправки, неполный ответ или падение после provider success до commit могут оставить внешний объект без локального подтверждения.

| Исход | Действие приложения |
| --- | --- |
| Доказан сбой до внешнего эффекта | Ограниченный retry по policy с backoff, тем же логическим действием и подходящим provider idempotency key |
| Provider явно отклонил действие | Сохранить классификацию; исправление контента/прав не подменяет старую попытку |
| UNKNOWN | Остановить повтор; выполнить supported read-only reconciliation либо запросить решение оператора с доказательством |
| Подтверждён success, повторное событие очереди | Вернуть уже записанный результат без внешнего вызова |
| Противоречивое/позднее подтверждение | Сохранить факт с привязкой к исходной попытке, проверить identity и сверить; не удалять его из-за новой revision или отмены intent |

Outbox даёт at-least-once доставку команды, не exactly-once внешний эффект. Provider idempotency используется только там, где поддержка и срок действия ключа подтверждены. Если lookup/idempotency недоступны, UNKNOWN требует ручной сверки; «не найдено по одному запросу» не всегда доказывает отсутствие объекта. После доказанного отсутствия создаётся новая разрешённая попытка, а не переписывается результат старой.

Отмена останавливает будущие claims, но не может отозвать уже отправленный сетевой запрос. Удаление опубликованного объекта, unpublish или исправление письма не являются rollback базы: это новые внешние действия с собственным согласованием. В reconciliation фиксируются actor, время, target и подтверждающие данные. Валидный поздний факт доставки нельзя молча отбросить как stale AI output.

## Credential isolation

- Frontend, AI Job Engine и обычные read-model запросы не имеют publishing secrets и не исполняют provider calls.
- Прикладной слой передаёт ID разрешённой операции; отдельный executor получает минимальный credential scope для утверждённого provider/target.
- Секреты не помещаются в PostgreSQL business rows, queue payloads, prompts, traces, exception text, URL query logs или Git. В БД допустима непривилегированная ссылка на конфигурацию секрета.
- Ротация сохраняет разрешённый target и проверенную identity; истёкший/неверный credential даёт безопасную ошибку без автоматической смены аккаунта.
- Проверка готовности, credential probe и фактическая отправка — разные операции. Успешный heartbeat worker или включённый provider flag не доказывают ни готовность канала, ни доставку поста.
- Email recipients и бизнес-данные передаются только нужному provider по согласованной privacy policy; suppression/отписки проверяются перед отправкой, даже при immutable audience snapshot.

## Проверяемость

Будущие implementation PR обязаны покрыть duplicate event, потерю broker после DB commit, падение до/после provider call, UNKNOWN recovery, revocation/cancellation race, позднее подтверждение, wrong target, stale revision, лимиты расходов и timezone/DST. Изолированные mocks/fake providers предшествуют отдельно разрешённому canary. PR-A0 не запускает даже canary.
