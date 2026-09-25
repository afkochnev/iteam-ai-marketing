# Architecture

The MVP is a modular monolith: Next.js calls a FastAPI REST API; PostgreSQL stores business state; Redis brokers background Celery jobs. Agent execution will be added behind the backend service layer in later iterations and will never expose OpenAI credentials to the browser.
# MVP architecture principles

The application is the orchestrator and PostgreSQL is the source of truth.

- LLM proposes structured output; application validates and persists it.
- Human approval is required before approved content can move forward.
- Agents receive isolated capabilities and immutable source/version allowlists.
- Content provenance points to immutable article/knowledge versions.
- Celery performs AI execution; HTTP endpoints do not wait for model calls.
- Dispatcher, row locks and generation keys provide idempotency.
- Recovery, timeout and cancellation paths reject stale late results.
- There is no external publication integration in MVP 1.
