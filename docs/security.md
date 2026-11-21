# Security review

> **Current-security overview with legacy history.** Этот документ возник на раннем MVP и не перечисляет все принятые provider/release controls. Актуальные operational boundaries дополнительно описаны в [normal operations](normal-operations.md), provider-specific runbooks и release documentation. Целевая Marketing OS security model имеет статус Proposed и описана в [architecture-v2](architecture-v2/vision.md) и [external actions](architecture-v2/external-actions.md).

- Authentication is an HttpOnly JWT cookie. Production requires
  `COOKIE_SECURE=true` and HTTPS; SameSite=Lax is retained. State-changing
  browser requests are constrained to configured CORS origins.
- ADMIN manages agent configuration and system diagnostics. MANAGER can use the
  permitted campaign/content workflow but cannot change prompts, models, tools
  or operational settings. Unauthenticated requests receive 401.
- CORS and trusted hosts are explicit in production. Security headers include
  nosniff, frame protection, referrer policy, Permissions-Policy and a minimal
  API CSP. HSTS is emitted in production and should also be configured at the
  TLS reverse proxy.
- Rate limiting protects login, AI actions and uploads. The current limiter is
  process-local; use a single backend instance for the MVP or replace it with a
  shared limiter before horizontal scaling.
- Uploads have an allowlist, size limit and sanitized display metadata. Files
  are not executed. Markdown is rendered without executable raw HTML.
- Request IDs are returned in `X-Request-ID`; errors expose safe messages only.
  Structured logging redacts authorization values, passwords, API keys, JWT
  secrets and database/Redis URL passwords.
- Agents receive only capability permissions and immutable runtime allowlists.
  LLM output is validated by the application, and human approval remains the
  boundary before content can move forward.
- AI agents, browser code and ordinary API/read-model processes do not receive
  external publishing credentials or send/publish tools. Dedicated publishing
  and metrics workers execute only application-authorized capabilities; write
  credentials and read-only metrics credentials remain isolated by role and
  exact provider target.
- Provider enablement, worker heartbeat and credential probe are distinct from
  execution approval and proof of delivery. Ambiguous external outcomes require
  reconciliation rather than blind retry; secrets are excluded from business
  rows, queue payloads, prompts, URLs, traces and safe error responses.

The current application is single-tenant. Cross-campaign authorization should
be revisited before operating it for multiple customers. The Proposed Marketing
OS must resolve workspace/RBAC, hypothesis/asset reuse, audience privacy,
provider credential scopes and experiment authority before the corresponding
A1/A3 implementation gates.
