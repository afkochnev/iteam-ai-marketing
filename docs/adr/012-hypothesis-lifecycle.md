# ADR 012: identity, версии и lifecycle маркетинговой гипотезы

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q13; documentation-only.

## Context

Гипотеза должна быть устойчивой единицей обучения, а не свободным текстом внутри Strategy. При этом не каждое уточнение формулировки является новой причинной идеей. Без правила версии результаты исследований, экспериментов и решений нельзя корректно сравнивать.

## Decision

1. `MarketingHypothesis` — стабильная identity одной причинной маркетинговой идеи внутри одной Campaign/Workspace.
2. `HypothesisVersion` — immutable проверяемая формулировка со следующими обязательными элементами:
   - primary audience/segment;
   - problem/need;
   - offer/message/intervention;
   - предполагаемый causal mechanism;
   - ожидаемое направление и размер KPI effect;
   - baseline/target/window;
   - assumptions и exclusions;
   - verification criterion;
   - evidence references и schema version.
3. Новая HypothesisVersion создаётся, когда уточняются формулировка, threshold, окно, assumptions, evidence links или segment boundaries, но сохраняются primary audience/problem, intervention и causal mechanism настолько, что прежние evidence остаются относимыми.
4. Новая MarketingHypothesis создаётся, если меняется primary causal mechanism, основной audience/problem, offer/intervention или desired business behavior так, что объединение evidence исказит вывод; также новая identity создаётся для параллельной альтернативной идеи.
5. StrategyVersion, ResearchVersion, AssetVersion, Experiment и Decision всегда фиксируют exact HypothesisVersion. Ссылка только на root/current version недостаточна.
6. Новая версия не перепривязывает существующие intents, experiments или decisions. Они сохраняют исходный subject.

## Lifecycle

`MarketingHypothesis` имеет operational lifecycle:

- `DRAFT` — identity создана, approved version ещё нет;
- `ACTIVE` — существует approved version, разрешённая для планирования проверки;
- `PAUSED` — новые действия временно не создаются, история/измерения продолжаются;
- `CLOSED` — проверка завершена или дальнейшая работа прекращена;
- `ARCHIVED` — скрыта из активной работы, история сохраняется.

HypothesisVersion не мутируется lifecycle-полем; approval и supersession хранятся отдельными append-only records/head projection.

## Evaluation result

Принятый human Decision по exact HypothesisVersion различает:

- `SUPPORTED` — критерий выполнен в пределах заявленных ограничений;
- `NOT_SUPPORTED` — полученные evidence не подтверждают заявленный критерий;
- `INCONCLUSIVE` — evidence недостаточны, stale, конфликтуют или качество не позволяет вывод;
- `SUPERSEDED` — проверка прекращена из-за новой версии/идеи без интерпретации как подтверждение или опровержение.

Decision не мутирует HypothesisVersion и не запускает corrective action автоматически. Следующая версия/identity, Strategy, Experiment или DistributionIntent создаются отдельной командой и approval.

## Head and concurrency

Допускается один current approved head pointer для UI, но он не является subject исполнения. Создание следующей версии использует expected head/version для optimistic concurrency. Одновременно подготовленные варианты могут существовать, однако активировать можно только явно выбранную approved version.

## Alternatives

- Любое изменение текста создаёт новую identity: дробит learning history.
- Все изменения остаются версиями одной hypothesis: объединяет несопоставимые causal ideas.
- Результат хранится прямо в HypothesisVersion: переписывает исходное ожидание задним числом.

## Consequences

Потребуются explicit commands `CreateHypothesis`, `CreateHypothesisVersion`, `ApproveHypothesisVersion`, `Activate/Pause/CloseHypothesis` и `RecordDecision`. Read model показывает identity, active version, evaluation result и freshness раздельно.

## Validation / links

Закрывает Q13 в [roadmap](../architecture-v2/roadmap.md). Дополняет ADR 008, [entity model](../architecture-v2/entity-model.md) и [golden journey](../architecture-v2/golden-journey.md).
