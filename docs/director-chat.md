# Marketing Director chat (PR25)

Open a Campaign and choose «Обсудить с Директором по маркетингу». Conversations
belong to their creator; ADMIN can inspect all. Archived campaigns and dialogs
retain readable history. The page polls only while an assistant answer is pending.

The application commits the user message, immutable campaign context, internal
MANUAL Task and QUEUED AgentRun before enqueueing the existing `execute_agent_run`
entrypoint on `ai`. The HTTP request never waits for a model. The Director's
PostgreSQL `settings.chat_prompt` is snapshotted into the run; its existing strategy
`system_prompt` remains unchanged. Migration and seed add the default only when
`chat_prompt` is absent, preserving customized values.

Chat gets **zero executable tools**, including read tools. The backend uses
CampaignWorkspaceService and the exact approved strategy Approval snapshot. Pending
strategies are identified separately. Each context includes an allowlist of entity
IDs. JSON is canonically hashed with SHA-256; a database trigger rejects snapshot
updates. Persisted history is retained in full. Model input has clearly delimited
snapshot, deterministic extractive digest, recent messages and current message.
User text and snapshot fields are untrusted data. Context limits are configured by
`DIRECTOR_CHAT_RECENT_MESSAGES` (12), `DIRECTOR_CHAT_CONTEXT_MAX_CHARS` (24000), and
`DIRECTOR_CHAT_DIGEST_MAX_CHARS` (8000). Oversized snapshots use an explicitly marked,
deterministically bounded projection; the full snapshot is still persisted.

Structured replies contain plain text, typed UUID references and limitations.
Unknown references are dropped while valid text is preserved; discard counts are
recorded in AgentRun output. The backend constructs local hrefs, and the frontend
accepts only their exact typed routes. Model HTML is displayed as plain text.
ActivityLog records identifiers and status metadata, never prompts or chat text.
Chat audit runs are readable by their conversation owner or ADMIN.

A conversation row lock and partial unique indexes serialize turns. Repeated
`client_message_id` returns the same persisted turn; another message while pending
returns `409 DIRECTOR_CHAT_TURN_IN_PROGRESS`. Terminal failures mark the assistant
FAILED without partial text. Explicit retry reuses the original user message,
snapshot, prompt/model and frozen runtime input, creates a fresh AgentRun and returns the same
assistant to PENDING. Duplicate retry clicks cannot create concurrent runs. Existing
AI recovery marks stranded running responses FAILED; history reads reconcile
terminal runs and stale unsubmitted queued runs. Internal tasks never participate
in business dependency propagation, business counts, default lists or dispatch.

No proposals, business approvals, content revisions, publication operations, KPI
analysis or live acceptance calls are included in PR25. Development and CI use
isolated PostgreSQL plus mocked model/queue results; provider DNS is blocked by
the backend test fixture.
