# Architecture

> **Legacy/current-production overview.** Этот документ начинался как описание раннего MVP и не является полным каталогом возможностей текущего production. Действующие runtime-режимы, publishing/metrics и release procedures описаны в [normal operations](normal-operations.md), provider runbooks и stage acceptance documents. Предлагаемая целевая архитектура находится в [architecture-v2](architecture-v2/vision.md). Формулировки ниже сохраняют исторический контекст и не должны интерпретироваться как запрет уже принятых интеграций.

The application remains a modular monolith: Next.js calls a FastAPI REST API; PostgreSQL stores business state; Redis brokers background Celery jobs. AI execution, publishing and metrics run behind the backend service layer and never expose OpenAI or provider credentials to the browser.

# Architecture principles

The application is the orchestrator and PostgreSQL is the source of truth.

- LLM proposes structured output; application validates and persists it.
- Human approval is required before approved content can move forward.
- Agents receive isolated capabilities and immutable source/version allowlists.
- Content provenance points to immutable article/knowledge versions.
- Celery performs AI execution; HTTP endpoints do not wait for model calls.
- Dispatcher, row locks and generation keys provide idempotency.
- Recovery, timeout and cancellation paths reject stale late results.
- MVP 1 originally had no external publication integration. Current production includes separately controlled Telegram/VK publishing and read-only metrics with target binding, credential isolation, reconciliation and operator runbooks.

Новые Marketing OS contracts — `MarketingHypothesis`/`HypothesisVersion`, versioned assets, `DistributionIntent`, `DeliveryAttempt`, `ExternalPlacement`, transactional outbox и server-side read models — имеют статус **Proposed** и вводятся только additive migration по [architecture-v2](architecture-v2/vision.md).
