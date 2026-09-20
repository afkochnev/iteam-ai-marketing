# Architecture

The MVP is a modular monolith: Next.js calls a FastAPI REST API; PostgreSQL stores business state; Redis brokers background Celery jobs. Agent execution will be added behind the backend service layer in later iterations and will never expose OpenAI credentials to the browser.

