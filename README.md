# iTeam AI Marketing Department

Основа web-приложения для управляемого AI-отдела маркетинга iTeam. Репозиторий развивается последовательно по master specification. Текущий scope: **Итерация 1 — Database и Auth**.

## Архитектура

- `frontend`: Next.js + React + TypeScript.
- `backend`: FastAPI; здесь будут REST API, бизнес-логика и интеграции.
- `worker`: Celery worker из того же backend image.
- `postgres`: основное хранилище будущих бизнес-сущностей.
- `redis`: broker/result backend фоновых задач.

## Требования

- Docker Engine с Docker Compose либо Python 3.12 и Node.js 22 для запуска без контейнеров.

## Локальный запуск

```bash
cp .env.example .env
docker compose up --build
docker compose exec backend alembic upgrade head
docker compose exec backend python -m app.seed
```

После запуска:

- frontend: http://localhost:3000
- backend health: http://localhost:8000/health
- OpenAPI: http://localhost:8000/docs

Откройте frontend и войдите с `ADMIN_EMAIL`/`ADMIN_PASSWORD` из `.env`. Повторный seed безопасен и не меняет существующему Admin пароль.

Остановить сервисы: `docker compose down`. Данные PostgreSQL и Redis сохраняются в named volumes.

## Backend без Docker

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
uvicorn app.main:app --reload
```

Проверки:

```bash
ruff check .
mypy app
pytest
```

## Frontend без Docker

```bash
cd frontend
npm ci
npm run dev
```

Проверки:

```bash
npm run lint
npm test
npm run build
```

## Worker

```bash
cd backend
celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
```

## Миграции и seed

Схема изменяется только через Alembic; приложение не вызывает `create_all()`.

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend alembic downgrade base
docker compose exec backend python -m app.seed
```

Первая обратимая migration создаёт `users`, PostgreSQL enum `user_role` и case-insensitive unique index по `lower(email)`.

## Authentication

Backend выпускает короткоживущий JWT с `sub` и `exp`, но JavaScript его не получает: токен хранится в `HttpOnly`, `SameSite=Lax` cookie. В production установите `COOKIE_SECURE=true` и используйте HTTPS. Logout удаляет cookie; blacklist не используется, поэтому уже выданный JWT остаётся криптографически валидным до истечения срока, но браузер после logout его больше не отправляет. Для чувствительных операций backend каждый раз загружает пользователя из БД и проверяет `is_active`.

Endpoints:

- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me`
- `POST /api/v1/auth/logout`

## AI Agents

Система содержит четыре фиксированные конфигурационные роли: Marketing Director, Knowledge Keeper, Writer и SMM Manager. Конфигурация агента — prompt, model override, status, autonomy level, settings и tool permissions — хранится в PostgreSQL. Именно БД является будущим runtime source of truth.

Default prompts находятся в `backend/app/prompts/*.md` и используются только при первом создании агента. Команда:

```bash
docker compose exec backend python -m app.seed
```

- создаёт отсутствующие system agents;
- добавляет только отсутствующие default tools;
- не перезаписывает изменённые prompt, model, status, autonomy level или description;
- не включает обратно отключённые tools и не сбрасывает `requires_approval`/settings.

Admin может изменять конфигурацию и tool permissions на `/agents`; Manager имеет read-only доступ. Реального Agent Runtime, OpenAI API и исполняемых tool-функций в этой итерации нет.

Agents API:

- `GET /api/v1/agents`
- `GET /api/v1/agents/{id}`
- `PATCH /api/v1/agents/{id}` — только Admin
- `PATCH /api/v1/agents/{agent_id}/tools/{tool_id}` — только Admin

## Environment

Скопируйте `.env.example` в `.env`, замените `JWT_SECRET`, `ADMIN_PASSWORD` и остальные placeholder-значения. Не коммитьте реальные секреты. `OPENAI_API_KEY` пока не используется.

## Тестовая база данных

Backend integration tests должны выполняться только против отдельной БД. В CI используется `iteam_test` в отдельном PostgreSQL service container. Локальный пример:

```bash
docker compose exec postgres createdb -U iteam iteam_test
docker run --rm --user root --network iteam-ai-marketing_default \
  -e DATABASE_URL=postgresql+asyncpg://iteam:iteam@postgres:5432/iteam_test \
  -v "$PWD/backend:/workspace" -w /workspace iteam-ai-marketing-backend \
  sh -c 'pip install -r requirements-dev.lock && alembic upgrade head && pytest'
```

## Типовые ошибки

- `frontend` ждёт нездоровый `backend`: проверьте `docker compose logs backend`.
- порты 3000/8000 заняты: освободите их или измените port mapping в Compose.
- после изменения dependency lock-файла пересоберите images с `docker compose build --no-cache`.
