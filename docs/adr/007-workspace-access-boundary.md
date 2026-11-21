# ADR 007: workspace boundary и capability-based access

- Status: **Accepted**.
- Date: 2026-11-21.
- Scope: A1 contracts, решение Q1; documentation-only.

## Context

Текущая система имеет глобальные роли `ADMIN` и `MANAGER`, а `Campaign.created_by` фиксирует автора, но не является границей владения или tenant isolation. Target architecture должна однозначно отвечать, какие кампании, версии, доставки и доказательства может видеть пользователь, и не должна позже добавлять `workspace_id` как необязательное поле поверх уже созданных vNext-данных.

Одновременно продукт пока эксплуатируется одной организацией. Полноценный self-service multi-tenant, биллинг и межорганизационный marketplace не нужны для первого вертикального среза.

## Decision

1. `Workspace` становится корневой security boundary для всех новых vNext-aggregate.
2. В первом production scope существует ровно один заранее созданный default workspace. Self-service создание workspace и перенос данных между workspace не входят в A1.
3. `Campaign` принадлежит ровно одному workspace. `MarketingHypothesis`, Research, Strategy, Asset, DistributionIntent, Observation, Decision, AIJob и их версии получают обязательный `workspace_id`; campaign-bound записи дополнительно получают обязательный `campaign_id`.
4. Человек получает доступ через `WorkspaceMembership`. Текущие глобальные роли мигрируют совместимо:
   - `ADMIN` → membership `WORKSPACE_ADMIN`;
   - `MANAGER` → membership `MARKETING_MANAGER`.
5. Начальный capability contract:

| Capability | WORKSPACE_ADMIN | MARKETING_MANAGER |
| --- | --- | --- |
| `workspace.read` | да | да |
| `campaign.manage` | да | да |
| `research.manage` | да | да |
| `asset.manage` | да | да |
| `subject.approve` | да | да |
| `execution.approve` для заранее настроенных targets | да | да |
| `provider_target.manage` | да | нет |
| `credential.manage` | да | нет |
| `membership.manage` | да | нет |
| `audit.read` | да | да |

6. Worker/service identity не является `WorkspaceMembership`. Runtime role получает узкую capability через конфигурацию процесса и не может использовать человеческое approval право.
7. Все repository/service queries vNext требуют явный `workspace_id`. Отсутствие scope или несовпадение workspace завершается отказом; фильтрация только во frontend недопустима.
8. Reuse между кампаниями в A1 выполняется через явное копирование/derivation точной версии в target campaign с provenance. Один mutable Asset, Strategy или Hypothesis не разделяется между кампаниями.
9. Cross-workspace links запрещены. Будущий экспорт/импорт создаёт новые identities и сохраняет source provenance, но не открывает исходные записи другому workspace.
10. `created_by` и `owner_user_id` остаются audit/operational metadata, но не заменяют workspace access check.

## Migration

Будущая additive migration создаёт default workspace и membership для существующих активных пользователей. Legacy tables продолжают использовать текущие проверки до cutover; новые таблицы не имеют nullable `workspace_id`. Backfill выполняется детерминированно, проверяется counts/FK и не меняет business state вручную.

## Alternatives

- Сохранить глобальные роли без workspace: проще сейчас, но делает будущую изоляцию небезопасным массовым retrofit.
- Сразу реализовать multi-tenant self-service: преждевременно увеличивает RBAC, billing и support scope.
- Использовать `created_by` как owner boundary: не поддерживает командную работу, делегирование и сервисные процессы.

## Consequences

Потребуются `workspaces`, `workspace_memberships`, scope-aware repositories и тесты cross-workspace denial. Первый production остаётся single-workspace, но новые данные уже имеют явную границу. Provider credentials и targets конфигурируются на workspace уровне, при этом секреты по-прежнему не выдаются API, браузеру или AI workers.

## Validation / links

Закрывает Q1 в [roadmap](../architecture-v2/roadmap.md). Связано с [domain map](../architecture-v2/domain-map.md), [entity model](../architecture-v2/entity-model.md) и ADR 010.
