# Roadmap и открытые архитектурные вопросы

Статус: **Target architecture / Proposed**. Этапы определяются результатом и gates, не календарным обещанием. Каждый implementation PR имеет собственный scope, tests, review и release approval. A0 ничего не развёртывает.

## Последовательность развития

| Этап | Результат | Exit gate |
| --- | --- | --- |
| PR-A0 — Architecture Baseline | Семь architecture документов, ADR 001–006, legacy/current banners и короткий README entry | Documentation-only diff от актуального main, согласованная терминология, открытые вопросы и Non-goals |
| A1 — Contracts & additive foundation | Модули/контракты, `MarketingHypothesis`/`HypothesisVersion`, version identities, intent/attempt/result schema, AIJob и outbox foundation; отдельные малые PR | Закрыты блокирующие Q1–Q5 и Q13; additive compatibility и failure tests; старый writer остаётся владельцем legacy |
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

## Открытые архитектурные вопросы

Ответы фиксируются последующими ADR; до ответа defaults ниже являются ограничениями, а не скрытым выбором поставщика. Владельцы — роли для согласования, не назначенные этим PR люди.

| ID | Вопрос | Кто принимает решение / когда блокирует | До решения |
| --- | --- | --- | --- |
| Q1 | Workspace/tenant boundary, владение кампаниями и допустимость reuse между ними? | Product owner + security; до A1 identities/RBAC | Сохранять текущие права; не открывать cross-workspace доступ |
| Q2 | Где граница AssetVersion, StrategyVersion, ResearchVersion и HypothesisVersion; как типизировать cross-version provenance? | Architecture + domain owners; до A1 схемы | Только immutable typed references, без unconstrained «любого JSON ID» |
| Q3 | Какие canonical action kinds, deduplication scopes и email batch/recipient cardinalities? | Distribution + email owner; core до A1, email детали до A3c | Не считать retry новым intent; acceptance не называть recipient delivery |
| Q4 | Какие изменения времени/creative/target/audience требуют нового execution approval, кто вправе делегировать approval? | Product + security; до A1 policy и A3b/A3c действий | Никакого расширения scope молча; text/target/audience строго привязаны к approval |
| Q5 | AIJob retry/cancel/budget semantics, модель стоимости и границы уже оплаченного разрешения? | AI runtime owner + product; до A1/A3a | Конечные лимиты, explicit authorization и отсутствие внешних write tools у AI |
| Q6 | Первая CMS и email platform, sandbox, target identity и способы safe lookup/idempotency? | Integration + operations + product; до A3b/A3c adapter | Не подключать providers или credentials в A0; UNKNOWN без доказательства не повторять |
| Q7 | Место хранения binaries, immutable object versioning, creative rights/provenance и retention? | Asset owner + security/operations; до A3b illustration | Hash + immutable reference; не обещать конкретный object store |
| Q8 | Источник бизнес-KPI, квалификация lead, deduplication, attribution window/model и privacy/consent для email/CRM? | Marketing owner + analytics + privacy; до A3c email и A3d measurement | Согласовать определение до запуска; missing не считать zero; не хранить recipients в общей очереди |
| Q9 | SLO/freshness read projections, outbox lag, job/attempt retention и reconciliation escalation thresholds? | Operations + architecture; до A3b production acceptance и окончательно до A3d | Показывать stale/unknown и as-of; не объявлять heartbeat доказательством доставки |
| Q10 | Какой первый cutover scope, mapping historical evidence, drain barrier и rollback после новых действий? | Release owner + distribution; до A2 backfill/A4 cutover | Один writer на scope; сохранить legacy write path до подтверждённого cutover |
| Q11 | Где физически изолирован executor credentials и как выполняется rotation/audit для новых providers? | Security + operations; до A3b/A3c внешнего доступа | Сохранять текущую изоляцию и targets; не передавать секреты AI/API/browser |
| Q12 | Какой эксперимент допустим: assignment unit, stop rules, statistical evidence и отдельные external approvals? | Marketing owner + analytics; до исполнения A3d corrective experiment | Разрешено принять Decision; запуск и изменения сайта не выполняются автоматически |
| Q13 | Когда изменение формулировки создаёт следующую HypothesisVersion, а когда новую MarketingHypothesis; какие lifecycle/result states допустимы? | Marketing owner + architecture + analytics; до A1 модели и A3a workflow | Stable hypothesis identity + immutable versions; Decision не мутирует прошлую версию; неопределённость не считается подтверждением или опровержением |

## Non-goals PR-A0

Этот PR не меняет runtime, backend/frontend, Alembic migrations, CI, Docker/Compose, providers, `.env`, credentials или operational topology. Не выполняет deployment, бизнес-операции, backfill, AI/сбор метрик, merge PR62/PR63 или live acceptance golden journey. Не выбирает все внешние платформы и не реализует ни одну из новых сущностей.

За рамками target baseline также находятся big-bang rewrite, микросервисное разбиение без доказанной необходимости, обещание exactly-once удалённого эффекта и автономное применение AI-решений. Следующие этапы сохраняют [immutable release pipeline](../stage-2-release-pipeline.md).

## Review checklist A0

- База — exact свежеполученный main SHA, а не PR62/PR63; состояние этих PR зафиксировано как наблюдение во времени.
- Diff содержит только architecture/ADR/current-documentation Markdown files и короткое дополнение README.
- Current production отделён от target; новые capabilities не выданы за уже работающие.
- `MarketingHypothesis`/`HypothesisVersion` являются first-class entities и связывают research, strategy, assets, experiment и Decision.
- Все шесть этапов DISCOVER → DECIDE → CREATE → DELIVER → MEASURE → LEARN и все четыре доставки есть в golden journey.
- A3 разделена на независимо поставляемые A3a–A3d; golden journey остаётся общей итоговой приёмкой, а не big-bang release.
- Разделены content approval/execution approval, intent/attempt/result, facts/AI proposals/human decisions и business KPI/channel diagnostics.
- Миграция additive, один writer на scope, старые данные не переписаны, legacy removal вынесен в поздний release; big-bang rewrite запрещён.
- Локальные ссылки и Mermaid читаемы; открытые решения имеют ID, владельца и блокирующий gate.
