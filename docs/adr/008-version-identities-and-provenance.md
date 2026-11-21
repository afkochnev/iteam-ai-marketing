# ADR 008: границы версий и typed provenance

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q2; documentation-only.

## Context

В legacy-модели часть важных связей восстанавливается из `metadata`/JSON, current pointers и косвенных признаков. Это допустимо для compatibility layer, но не для нового ядра: approval, доставка, эксперимент и решение должны ссылаться на точную immutable версию и иметь проверяемую referential integrity.

## Decision

1. Стабильная identity и immutable version разделяются для четырёх основных семейств:
   - `MarketingHypothesis` / `HypothesisVersion`;
   - `Research` / `ResearchVersion`;
   - `Strategy` / `StrategyVersion`;
   - `Asset` / `AssetVersion`.
2. Каждая version принадлежит ровно одной root identity и имеет уникальный `(root_id, version_number)`, `schema_version`, canonical snapshot hash, provenance actor/source и `created_at`.
3. Version row не изменяется после создания. Исправление создаёт следующую version с `supersedes_version_id`; техническая пометка invalid/retracted хранится отдельным append-only решением, а не переписывает payload.
4. `current_version_id` допускается только как удобный projection/head pointer. Approval, IntentRevision, Experiment, Observation attribution и Decision никогда не используют его как subject.
5. Авторитетные cross-version links являются типизированными FK, а не произвольными `object_type/object_id` в JSON. Минимальные relation contracts:
   - ResearchVersion ↔ HypothesisVersion с evidence relation kind;
   - StrategyVersion ↔ HypothesisVersion;
   - AssetVersion ↔ HypothesisVersion;
   - AssetVersion ↔ AssetVersion для derivation/adaptation;
   - Experiment/Decision ↔ exact HypothesisVersion;
   - IntentRevision ↔ exact AssetVersion.
6. Relation kind берётся из allowlist и имеет собственную schema version. Произвольная строка не создаёт новый domain relation без изменения contract.
7. Все стороны typed link обязаны принадлежать одному workspace. В A1 campaign-bound связи также принадлежат одной Campaign. Cross-campaign reuse создаёт derived identity/version в target campaign и explicit provenance link.
8. Canonical hash вычисляется над нормализованным typed snapshot с указанной schema version. Hash не заменяет FK, но защищает exact subject и audit evidence.
9. Binary creative content хранится по immutable object version/hash; изменяемый URL или путь не является AssetVersion identity.
10. Legacy `ContentItem`/`ContentVersion`, JSON metadata и polymorphic Approval продолжают работать в legacy writer до cutover. Adapter создаёт typed vNext reference без silent reinterpretation.

## Physical guidance for A1

Отдельные relation tables предпочтительнее универсальной polymorphic relation table. Если повторяющиеся поля вынесены в общий mixin, это не отменяет конкретные FK и DB constraints. Version sequence выделяется транзакционно и покрывается конкурентными тестами.

## Alternatives

- Один generic `version_links(object_type, object_id)`: уменьшает число таблиц, но теряет FK и допускает недействительные комбинации.
- Mutable JSON snapshot с числом версии: сохраняет историю частично, но позволяет незаметно менять старое содержимое.
- Ссылка только на root identity: снова вводит выбор latest во время исполнения.

## Consequences

Схема станет более явной и потребует нескольких relation tables, зато большая часть Python-проверок происхождения перейдёт в FK/unique/check constraints. Backfill может пометить legacy provenance как `IMPORTED_INCOMPLETE`, не выдумывая отсутствующие связи.

## Validation / links

Закрывает Q2 в [roadmap](../architecture-v2/roadmap.md). Дополняет [ADR 003](003-versioned-assets.md), [entity model](../architecture-v2/entity-model.md) и ADR 012.
