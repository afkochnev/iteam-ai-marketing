# iTeam AI Marketing Department

Основа web-приложения для управляемого AI-отдела маркетинга iTeam. Репозиторий развивается последовательно по master specification. Текущий scope: **Итерация 5 — Agent Runtime**.

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

Миграции последовательно создают пользователей, конфигурацию агентов, Campaigns, Tasks, AgentRuns и ToolCalls. Все миграции обратимы; актуальность ORM metadata проверяется командой `alembic check`.

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

Admin может изменять конфигурацию и tool permissions на `/agents`; Manager имеет read-only доступ. Runtime использует сохранённые в БД prompt/model snapshots, но исполняемых tool-функций в текущей итерации ещё нет.

Agents API:

- `GET /api/v1/agents`
- `GET /api/v1/agents/{id}`
- `PATCH /api/v1/agents/{id}` — только Admin
- `PATCH /api/v1/agents/{agent_id}/tools/{tool_id}` — только Admin

## Campaigns

Campaign — маркетинговая инициатива и будущий родитель для стратегии, задач, контента и согласований. В этой итерации Campaign хранит бизнес-контекст, период, автора и полный lifecycle status enum. Поле `strategy` зарезервировано для Marketing Director и недоступно в формах и обычном PATCH.

Поддерживаются статусы `DRAFT`, `PLANNING`, `WAITING_APPROVAL`, `ACTIVE`, `PAUSED`, `COMPLETED` и `ARCHIVED`. Сейчас пользовательский workflow намеренно ограничен: новая Campaign всегда создаётся как `DRAFT`, а единственная доступная lifecycle-операция переводит любое неархивное состояние в `ARCHIVED`. Произвольное изменение status запрещено. Архивирование идемпотентно, архивные записи доступны только для чтения и физически не удаляются.

Список `/campaigns` по умолчанию не показывает архивные записи; фильтр статуса позволяет отдельно выбрать `ARCHIVED` или любой другой status. Список упорядочен по дате создания, затем по UUID.

Campaign API (Admin и Manager):

- `GET /api/v1/campaigns?status=DRAFT`
- `POST /api/v1/campaigns`
- `GET /api/v1/campaigns/{id}`
- `PATCH /api/v1/campaigns/{id}`
- `POST /api/v1/campaigns/{id}/archive`

Проверки Campaigns входят в обычные backend/frontend test suites. Для отдельного backend-прогона используйте `pytest app/tests/test_campaigns.py` после подготовки тестовой PostgreSQL по инструкции ниже.

## Task Workflow

Task — универсальная единица работы внутри Campaign. Задача имеет тип (`CAMPAIGN_PLANNING`, `KNOWLEDGE_RESEARCH`, `WRITE_ARTICLE`, `CREATE_SOCIAL_POSTS`, `CONTENT_REVISION` или `MANUAL`), приоритет, назначенного агента, входные/выходные JSON-данные и lifecycle status.

Статусы: `NEW`, `BLOCKED`, `READY`, `IN_PROGRESS`, `WAITING_REVIEW`, `WAITING_APPROVAL`, `APPROVED`, `COMPLETED`, `FAILED`, `CANCELLED`. Обычная созданная задача сразу становится `READY`, если у неё нет незавершённых зависимостей, иначе — `BLOCKED`. Только `COMPLETED` считается успешным terminal status и разблокирует downstream-задачи; `FAILED`, `CANCELLED` и `APPROVED` не разблокируют их.

Dependencies определяют порядок выполнения и не смешиваются с иерархией `parent_task_id`. Service layer запрещает self-, cross-Campaign и cyclic dependencies. При завершении задачи статусы всех непосредственно зависимых задач пересчитываются в той же транзакции. Удаление последней незавершённой dependency также переводит задачу в `READY`.

Task API:

- `GET/POST /api/v1/tasks`
- `GET/PATCH /api/v1/tasks/{id}`
- `POST /api/v1/tasks/{id}/start`
- `POST /api/v1/tasks/{id}/complete`
- `POST /api/v1/tasks/{id}/cancel`
- `POST /api/v1/tasks/{id}/dependencies`
- `DELETE /api/v1/tasks/{id}/dependencies/{dependency_task_id}`

Manual start/complete endpoints остаются доступны для проверки workflow engine. Для `MANUAL` Task с назначенным активным агентом также доступен реальный фоновый AI-run; остальные TaskType намеренно заблокированы до соответствующих бизнес-итераций.

## Agent Runtime

Backend использует зафиксированный `openai-agents==0.10.5`. SDK выполняет OpenAI-модели через Responses API. HTTP endpoint только валидирует запрос, создаёт `AgentRun` со статусом `QUEUED` и ставит Celery job в Redis; вызов `await Runner.run(...)` выполняется worker-процессом вне HTTP request-response цикла.

Runtime универсален: он загружает из PostgreSQL prompt, model override и enabled tool permissions назначенного агента. Фактическая модель выбирается из `agents.model`, затем из `OPENAI_DEFAULT_MODEL`. System prompt и его SHA-256 сохраняются в каждом AgentRun. Task context передаётся как user input и не дописывается в system prompt.

Lifecycle:

```text
Task READY → AgentRun QUEUED → RUNNING → COMPLETED/FAILED/CANCELLED
                Task IN_PROGRESS → COMPLETED/FAILED/CANCELLED
```

Успешное завершение использует существующий dependency resolver и переводит доступные downstream Tasks из `BLOCKED` в `READY`. Partial unique index PostgreSQL допускает только один активный run (`QUEUED`, `RUNNING`, `WAITING_APPROVAL`) на Task. Повторная Celery delivery идемпотентна. При отмене Task во время model call поздний ответ сохраняется только в AgentRun и не переводит Task обратно в `COMPLETED`.

Ограничения runtime задаются `AGENT_MAX_TURNS`, `AGENT_RUN_TIMEOUT_SECONDS` и `AGENT_MAX_RETRIES`. Retry — явная операция для `FAILED` Task, создающая новый AgentRun без удаления истории. Usage (`request_count`, input/output/total tokens) сохраняется после успешного SDK run; `estimated_cost` пока остаётся `null`.

SDK tracing регулируется `OPENAI_AGENTS_DISABLE_TRACING`. Trace metadata содержит только campaign/task/agent/run identifiers, а передача sensitive inputs/outputs в tracing отключена. Собственные AgentRun и ToolCall остаются бизнес-аудитом приложения. Tool registry пока пуст: runtime передаёт модели только пересечение enabled permissions и реально зарегистрированных implementations, поэтому будущие seeded permissions не ломают `MANUAL` run.

Agent Runtime API (Admin и Manager):

- `POST /api/v1/tasks/{id}/run`
- `POST /api/v1/tasks/{id}/retry`
- `GET /api/v1/agent-runs?task_id=...&agent_id=...&campaign_id=...&status=...`
- `GET /api/v1/agent-runs/{id}`

Для ручного live smoke test настройте `OPENAI_API_KEY` и `OPENAI_DEFAULT_MODEL`, создайте `MANUAL` Task с активным Agent и нажмите «Запустить AI». В CI и обычном `pytest` `Runner.run` подменяется; реальные API credits не расходуются.

**Iteration 5 доказывает только универсальное AI-выполнение.** Наличие Marketing Director, Knowledge Keeper, Writer и SMM Manager в БД ещё не означает, что они выполняют свои бизнес-типы задач. CampaignPlan, knowledge search, создание статей и SMM появятся в следующих итерациях.

## Environment

Скопируйте `.env.example` в `.env`, замените `JWT_SECRET`, `ADMIN_PASSWORD` и остальные placeholder-значения. Не коммитьте реальные секреты. `OPENAI_API_KEY` передаётся только backend и worker через server-side `.env`; frontend получает только `NEXT_PUBLIC_API_URL`.

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
