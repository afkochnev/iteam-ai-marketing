# Roadmap и открытые архитектурные вопросы

Статус: **Target architecture / Proposed**. Этапы определяются результатом и gates, не календарным обещанием. Каждый implementation PR имеет собственный scope, tests, review и release approval. A0 ничего не развёртывает.

## Последовательность развития

| Этап | Результат | Exit gate |
| --- | --- | --- |
| PR-A0 — Architecture Baseline | Семь architecture документов, ADR 001–006, legacy/current banners и короткий README entry | Documentation-only diff от актуального main, согласованная терминология, открытые вопросы и Non-goals |
| A1 — Contracts & additive foundation | Модули/контракты, `Workspace`, `MarketingHypothesis`/`HypothesisVersion`, version identities, intent/attempt/result schema, AIJob и outbox foundation; отдельные малые PR | ADR 007–012 приняты; additive compatibility и failure tests; старый writer остаётся владельцем legacy |
| A2 — Read models & shadow evidence | Серверная картина campaign/hypothesis/asset/delivery, legacy mapping и проверяемый backfill | Parity на репрезентативных fixtures, правильные UNKNOWN/stale/zero, права и bounded queries; нет внешних эффектов |
| A3a — Discover & Decide | Одно competitor/SEO исследование → exact HypothesisVersion → StrategyVersion | Версии, provenance, approvals и AI budgets покрыты; provider writes отсутствуют |
| A3b — Create & Website | Одна статья + одна illustration → CMS draft/public placement через отдельно выбранный adapter | Exact versions, creative rights, outbox, attempts, UNKNOWN/reconciliation и sandbox acceptance подтверждены |
| A3c — Multichannel Delivery | Одно email + один VK-пост + один Telegram-пост как самостоятельные адаптации | Audience/consent policy, exact targets, idempotency, test send/canaries и credential isolation подтверждены отдельно по provider |
| A3d — Measure & Learn | Website/email/social/CRM observations → один KPI evaluation → Decision по exact HypothesisVersion | [Golden journey](golden-journey.md) пройден целиком на mocks; evidence freshness, attribution limits и decision boundary проверены |
| A4 — Controlled cutover | Переключение writer для явно выбранного scope, затем расширение по одному scope | [Cutover gate](migration-strategy.md#cutover-gate), drain/сверка, monitoring, совместимый rollback и отдельный release approval |
| A5 — Measurement & learning depth | Более полные analytics/attribution, SEO/competitor monitoring, управляемые experiments | Доказанные источники, freshness, версии модели attribution, policy/consent и интерпретируемые evidence |
| A6 — Consolidation | Удаление доказанно неиспользуемых legacy частей отдельным поздним release | Закрыто окно совместимости, проверены retention/restore и все consumers; независимое approval cleanup |

A3a–A3d — независимо поставляемые вертикальные срезы, а не один большой release. Единый golden journey остаётся итоговой acceptance boundary всей A3. Каждый срез может быть принят, развёрнут и откатан без подключения ещё не готовых providers; на каждой scope boundary действует один writer.

AI Job Engine и credential isolation входят в foundation и соответствующие срезы, а не откладываются до подключения всех каналов. Метрики и принятое корректирующее решение нужны уже в A3d; A5 расширяет глубину, а не впервые добавляет MEASURE/LEARN. CMS/email providers выбираются до реализации соответствующего adapter, не этим документом.

## Решения блокеров A1

| ID | Решение | ADR | Статус |
| --- | --- | --- | --- |
| Q1 | Один explicit Workspace как security root; initial production single-workspace; membership/capabilities и deny-by-default cross-workspace | [ADR 007](../adr/007-workspace-access-boundary.md) | Resolved |
| Q2 | Stable roots + immutable typed versions; explicit FK relation tables; current pointers не являются subject | [ADR 008](../adr/008-version-identities-and-provenance.md) | Resolved |
| Q3 | Allowlisted action kinds, stable business key, revision fingerprint, transport retry ≠ новый intent; email recipient events отдельно | [ADR 009](../adr/009-action-kinds-and-deduplication.md) | Resolved |
| Q4 | Subject approval отделён от execution approval; exact target/audience/payload/schedule scope; explicit capability delegation | [ADR 010](../adr/010-approval-scopes-and-delegation.md) | Resolved |
| Q5 | AIJob отделён от AIJobAttempt; immutable authorization/budget, bounded retry, cancellation и non-promotable late result | [ADR 011](../adr/011-ai-job-attempts-and-budgets.md) | Resolved |
| Q13 | Stable MarketingHypothesis, immutable versions, формальные правила новой version/identity и results | [ADR 012](../adr/012-hypothesis-lifecycle.md) | Resolved |

Эти решения закрывают contract blockers, но не создают таблицы или runtime. Физическая схема вводится несколькими additive implementation PR с отдельными миграциями, failure tests и rollback.

## Открытые архитектурные вопросы

Ответы фиксируются последующими ADR; до ответа defaults ниже являются ограничениями, а не скрытым выбором поставщика. Владельцы — роли для согласования, не назначенные этим PR люди.

| ID | Вопрос | Кто принимает решение / когда блокирует | До решения |
| --- | --- | --- | --- |
| Q6 | Первая CMS и email platform, sandbox, target identity и способы safe lookup/idempotency? | Integration + operations + product; до A3b/A3c adapter | Не подключать providers или credentials в A1; UNKNOWN без доказательства не повторять |
| Q7 | Место хранения binaries, immutable object versioning, creative rights/provenance и retention? | Asset owner + security/operations; до A3b illustration | Hash + immutable reference; не обещать конкретный object store |
| Q8 | Источник бизнес-KPI, квалификация lead, deduplication, attribution window/model и privacy/consent для email/CRM? | Marketing owner + analytics + privacy; до A3c email и A3d measurement | Согласовать определение до запуска; missing не считать zero; не хранить recipients в общей очереди |
| Q9 | SLO/freshness read projections, outbox lag, job/attempt retention и reconciliation escalation thresholds? | Operations + architecture; до A3b production acceptance и окончательно до A3d | Показывать stale/unknown и as-of; не объявлять heartbeat доказательством доставки |
| Q10 | Какой первый cutover scope, mapping historical evidence, drain barrier и rollback после новых действий? | Release owner + distribution; до A2 backfill/A4 cutover | Один writer на scope; сохранить legacy write path до подтверждённого cutover |
| Q11 | Где физически изолирован executor credentials и как выполняется rotation/audit для новых providers? | Security + operations; до A3b/A3c внешнего доступа | Сохранять текущую изоляцию и targets; не передавать секреты AI/API/browser |
| Q12 | Какой эксперимент допустим: assignment unit, stop rules, statistical evidence и отдельные external approvals? | Marketing owner + analytics; до исполнения A3d corrective experiment | Разрешено принять Decision; запуск и изменения сайта не выполняются автоматически |

## Non-goals PR-A0

Этот PR не меняет runtime, backend/frontend, Alembic migrations, CI, Docker/Compose, providers, `.env`, credentials или operational topology. Не выполняет deployment, бизнес-операции, backfill, AI/сбор метрик, merge legacy PR или live acceptance golden journey. Не выбирает все внешние платформы и не реализует ни одну из новых сущностей.

За рамками target baseline также находятся big-bang rewrite, микросервисное разбиение без доказанной необходимости, обещание exactly-once удалённого эффекта и автономное применение AI-решений. Следующие этапы сохраняют [immutable release pipeline](../stage-2-release-pipeline.md).

## Non-goals ADR 007–012

Decision pack не создаёт schema/API/UI, не меняет legacy roles, не выполняет backfill и не включает multi-tenant, CMS, email или новые providers. Он фиксирует contracts, которым должны соответствовать последующие A1 implementation PR.

## Review checklist A1 decisions

- Workspace является обязательной security boundary, но initial scope остаётся single-workspace.
- Cross-campaign reuse не создаёт общий mutable subject; cross-workspace links запрещены.
- Все business references указывают на exact immutable versions через typed FK.
- Canonical action kinds различают website draft/public/update, email test/campaign и social publish.
- Business deduplication отделена от transport retry; UNKNOWN блокирует повтор.
- Subject approval и execution approval имеют разные scope и invalidation rules.
- AIJob/Attempt, budget, cancellation и late result semantics определены.
- MarketingHypothesis/version/result lifecycle не мутирует прошлые evidence.
- Новых runtime capabilities, provider calls, migrations или deployment нет.
