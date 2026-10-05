# Architecture

The MVP is a modular monolith: Next.js calls a FastAPI REST API; PostgreSQL stores business state; Redis brokers background Celery jobs. AI execution and scheduling are separate from publication execution, scheduling, and metrics. The default runtime starts only AI automation, and the AI worker never receives Telegram/VK credentials or consumes publication queues. OpenAI credentials remain server-side and are never exposed to the browser.
# MVP architecture principles

The application is the orchestrator and PostgreSQL is the source of truth.

- LLM proposes structured output; application validates and persists it.
- Human approval is required before approved content can move forward.
- Agents receive isolated capabilities and immutable source/version allowlists.
- Content provenance points to immutable article/knowledge versions.
- Celery performs AI execution; HTTP endpoints do not wait for model calls.
- `ai` is the only queue consumed by the default AI worker; all other work has explicit routes and `unrouted` is never consumed.
- PostgreSQL row locks and active-run constraints coordinate dispatch; Redis is transport, never a lock.
- Dispatcher, row locks and generation keys provide idempotency.
- Recovery, timeout and cancellation paths reject stale late results.
- There is no external publication integration in MVP 1.
