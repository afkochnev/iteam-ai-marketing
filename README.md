# iTeam AI Marketing Department

Основа web-приложения для управляемого AI-отдела маркетинга iTeam. Репозиторий развивается последовательно по master specification. Текущий scope: **Итерация 6 — Marketing Director и согласование стратегии**.

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

SDK tracing регулируется `OPENAI_AGENTS_DISABLE_TRACING`. Trace metadata содержит только campaign/task/agent/run identifiers, а передача sensitive inputs/outputs в tracing отключена. Собственные AgentRun и ToolCall остаются бизнес-аудитом приложения. Runtime передаёт модели только пересечение enabled permissions и реально зарегистрированных implementations, поэтому будущие seeded permissions не ломают `MANUAL` run.

Agent Runtime API (Admin и Manager):

- `POST /api/v1/tasks/{id}/run`
- `POST /api/v1/tasks/{id}/retry`
- `GET /api/v1/agent-runs?task_id=...&agent_id=...&campaign_id=...&status=...`
- `GET /api/v1/agent-runs/{id}`

Для ручного live smoke test настройте `OPENAI_API_KEY` и `OPENAI_DEFAULT_MODEL`, создайте `MANUAL` Task с активным Agent и нажмите «Запустить AI». В CI и обычном `pytest` `Runner.run` подменяется; реальные API credits не расходуются.

**Iteration 5 доказывает универсальное AI-выполнение.** Специализированные типы включаются отдельно: CampaignPlan добавлен в Iteration 6, а knowledge search, создание статей и SMM остаются следующими этапами.

## Marketing Director Workflow

Marketing Director стал первым специализированным бизнес-агентом. Для Campaign в статусе `DRAFT` endpoint `POST /api/v1/campaigns/{id}/generate-strategy` атомарно переводит Campaign в `PLANNING`, создаёт `CAMPAIGN_PLANNING` Task, назначает системного агента `marketing_director` и ставит AgentRun в Celery.

SDK Agent использует `output_type=CampaignPlan`. Pydantic и application validators проверяют структуру, разрешённые типы/агентов, уникальность ключей, существование dependencies, отсутствие циклов и обязательную цепочку knowledge → article → social. Валидный результат сохраняется как JSON, увеличивает `strategy_version`, переводит Campaign в `WAITING_APPROVAL` и создаёт `PENDING` Approval со snapshot стратегии. Невалидный или failed run не меняет стратегию и не создаёт Approval.

Согласование поддерживает три операции:

- `POST /api/v1/campaigns/{id}/approve-strategy` — валидирует snapshot, проверяет активность downstream agents, создаёт Tasks/dependencies через TaskService и переводит Campaign в `ACTIVE`;
- `POST /api/v1/campaigns/{id}/request-strategy-revision` — сохраняет feedback, закрывает текущий Approval как `REVISION_REQUESTED` и автоматически запускает следующую planning Task;
- `POST /api/v1/campaigns/{id}/reject-strategy` — сохраняет причину, оставляет snapshot в истории и возвращает Campaign в `DRAFT`.

Approval history доступна через `GET /api/v1/approvals` и `GET /api/v1/approvals/{id}`. Partial unique index запрещает два pending approval одной версии. Row locks и metadata с generated Task IDs обеспечивают идемпотентное согласование без дублирования графа.

Архитектурный принцип workflow:

```text
LLM proposes.
Application validates.
Human approves.
Application executes.
```

Marketing Director не получает executable `create_internal_task`: AI только предлагает CampaignPlan. Бизнес-данные и Task graph изменяет application layer после human approval. Созданные `KNOWLEDGE_RESEARCH`, `WRITE_ARTICLE` и `CREATE_SOCIAL_POSTS` Tasks пока не исполняются AI-runtime — это scope следующих итераций.

## Knowledge Base

База знаний разделена на три бизнес-сущности. `KnowledgeStore` связывает приложение с одним активным OpenAI Vector Store, `KnowledgeSource` описывает происхождение материалов (в MVP — «Ручные загрузки»), а `KnowledgeItem` хранит локальную identity, provenance, внешние IDs и lifecycle документа. PostgreSQL хранит бизнес-метаданные и audit state; исходный файл и retrieval index находятся у OpenAI. Полный текст документов в PostgreSQL не дублируется.

Admin инициализирует Vector Store через `POST /api/v1/knowledge/store/initialize`. Если задан `OPENAI_VECTOR_STORE_ID`, существующий store проверяется и регистрируется; иначе создаётся `iTeam Knowledge Base`. Повторный вызов возвращает текущий active store. Source of truth после bootstrap — `knowledge_stores.external_store_id`.

Upload lifecycle:

```text
UPLOADING → OpenAI File → INDEXING → Vector Store → READY
                                      ↘ FAILED
READY/FAILED → ARCHIVED
```

Поддерживаются `.pdf`, `.docx`, `.txt` и `.md`, размер ограничивает `KNOWLEDGE_MAX_UPLOAD_MB`. HTTP endpoint не ждёт индексацию: worker получает только KnowledgeItem ID, attach выполняется идемпотентно, provider auto chunking остаётся включённым. Таймаут и polling задаются `KNOWLEDGE_INDEX_TIMEOUT_SECONDS` и `KNOWLEDGE_INDEX_POLL_INTERVAL_SECONDS`. Archive удаляет attachment из active Vector Store и исключает документ из retrieval, не удаляя локальный audit record или OpenAI File.

Knowledge API:

- `GET /api/v1/knowledge/store`, `GET /sources`, `GET /items`, `GET /items/{id}` — Admin и Manager;
- `POST /api/v1/knowledge/store/initialize`, `/upload`, `/items/{id}/retry`, `/items/{id}/archive` — только Admin;
- `POST /api/v1/knowledge/search` — Admin и Manager.

Поиск выполняется напрямую через OpenAI Vector Store Search, без LLM и без SQL LIKE. Каждый provider result сопоставляется с локальным READY KnowledgeItem по OpenAI file ID. Несопоставленные и архивные результаты отбрасываются. Ответ всегда содержит KnowledgeItem/source IDs, source title, filename, OpenAI file ID, excerpt и provider relevance score: provenance обязательна.

`search_knowledge` зарегистрирован как typed Agents SDK FunctionTool. Модели он доступен только при одновременно включённом `agent_tools.search_knowledge` и наличии implementation в registry. Каждый вызов создаёт `ToolCall` (`STARTED → COMPLETED/FAILED`) с аргументами и ограниченным structured result. Для ручного smoke runtime можно назначить `MANUAL` Task Knowledge Keeper; специализированный `KNOWLEDGE_RESEARCH` workflow намеренно остаётся заблокированным до следующей итерации.

Для live smoke задайте `OPENAI_API_KEY`, инициализируйте store, загрузите небольшой MD/TXT через `/knowledge`, дождитесь `READY`, выполните поиск по уникальному marker и затем архивируйте тестовый KnowledgeItem. Автоматические тесты мокируют Files, Vector Stores и Search и не расходуют API credits.

**Knowledge Base и search tool работают независимо от LLM; специализированный workflow описан ниже.**

## Knowledge Keeper Workflow

Knowledge Keeper выполняет `KNOWLEDGE_RESEARCH` через общий Agent Runtime. Запуск разрешён только системному агенту `knowledge_keeper`, если он активен, имеет enabled permission `search_knowledge`, implementation зарегистрирован в Tool Registry и существует active KnowledgeStore. Задача получает Campaign strategy, brief и business context, но не получает содержимое документов напрямую: знания доступны только через tool.

Structured output `KnowledgeResearchResult` содержит query, summary, sufficient flag, gaps и список выбранных `result_key`. Модель не возвращает filename, excerpt, score или локальные UUID источников. Каждый `result_key` детерминированно вычисляется приложением как SHA-256 от локального KnowledgeItem ID, OpenAI file ID и точного excerpt.

Архитектурный инвариант provenance:

```text
Vector Store Search
→ search_knowledge ToolCall.result
→ LLM выбирает только result_key
→ backend сверяет result_key и READY KnowledgeItem
→ KnowledgePackItem копирует provenance из ToolCall
```

`KnowledgeResearchResultProcessor` требует хотя бы один завершённый `search_knowledge` ToolCall, объединяет результаты всех поисковых вызовов текущего AgentRun и отклоняет неизвестные keys. Перед сохранением он повторно проверяет, что каждый KnowledgeItem остаётся `READY`; архивирование между retrieval и persistence приводит к `INVALID_KNOWLEDGE_SELECTION`, а не к молчаливой подмене результата.

При `sufficient=true` атомарно создаются READY KnowledgePack и KnowledgePackItems, Knowledge Task завершается и существующий dependency resolver переводит Writer Task из `BLOCKED` в `READY`. При `sufficient=false` создаётся исторический INSUFFICIENT pack, AgentRun остаётся `COMPLETED`, но business Task становится `FAILED` с `INSUFFICIENT_KNOWLEDGE`; downstream остаётся заблокированным. После добавления материалов обычный retry создаёт новый AgentRun и новый KnowledgePack, не удаляя историю.

Knowledge Pack API (Admin и Manager):

- `GET /api/v1/knowledge-packs?campaign_id=...&task_id=...&status=READY`
- `GET /api/v1/knowledge-packs/{id}`

Task UI показывает verified summary, gaps и provenance каждого фрагмента: source, filename, relevance score, excerpt и selection reason. Запуск остаётся ручным; автоматический dispatcher цепочки ещё не реализован. Writer и остальные специализированные исполнители по-прежнему заблокированы до следующих итераций.

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
## Writer Workflow

Writer выполняет только задачи `WRITE_ARTICLE`, назначенные активному агенту `writer`. Он не ищет документы во всей базе знаний: application layer вычисляет READY `KnowledgePack` из upstream research tasks и предоставляет Writer инструмент `read_knowledge_pack` только для этих пакетов. Каждый вызов аудируется, а все `knowledge_pack_item_ids` из `ArticleWritingResult` проверяются по фактическим результатам tool calls.

При достаточных материалах Writer создаёт `ContentItem` типа `ARTICLE`, первую `ContentVersion` и нормализованные `ContentVersionSource`, после чего завершает задачу и разблокирует downstream social task. При `sufficient=false` AgentRun остаётся завершённым, но бизнес-задача получает `INSUFFICIENT_ARTICLE_EVIDENCE`; статья не создаётся. Markdown рендерится детерминированно и показывается frontend без исполнения raw HTML.

## SMM Manager Workflow

SMM Manager выполняет `CREATE_SOCIAL_POSTS` только для активного агента `smm_manager`. Application layer передаёт ему разрешённые immutable версии статей, а агент получает полный текст только через `read_content_version`; поиск по Knowledge Base и инструменты публикации ему недоступны.

Результат валидируется как `SocialPostPackResult`: проверяются количество постов, каналы из стратегии кампании, порядок публикации и ссылки на реальные разделы исходной Article Version. Создаются отдельный `SOCIAL_POST_PACK`, индивидуальные `SOCIAL_POST` ContentItems и `ContentDerivation` к статье. Публикация наружу не выполняется.

Article и Social Post Pack имеют независимые `CONTENT_ITEM` approvals. Согласование проверяет immutable version snapshot, поддерживает approve/reject и для Pack атомарно меняет статусы дочерних постов. Approve не означает публикацию.
