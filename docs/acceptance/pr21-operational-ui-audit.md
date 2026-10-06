# PR #21 operational UI audit

Baseline: main, 22f869d5a7f77b40e7de4418b8aa5ef0a0ab0cf7. Isolated branch/worktree; production-like runtime and untracked DOCX untouched. Provider calls prohibited. Real-runtime browser navigation is read-only; mutating scenarios use a loopback fixture API with no database or provider connections.

## Findings recorded before fixes

| Screen/action | Expected | Actual at baseline | Severity | Root cause |
|---|---|---|---|---|
| Workspace next step / direct plan-item hash | Exact card scrolls into view after async load and is identifiable | Plan list elements have no matching id | P1 | Backend href contract not implemented by frontend |
| Create post | Create task, observe autonomous run | tasksApi.create followed by tasksApi.run; scheduler races produce false unconfirmed-start error | P1 | Pre-autonomous workflow retained |
| Refresh pending post workflow | Discover existing task/run and continue observation | Local postJobs state lost; polling requires manual response runId | P1 | Client-only state, no asynchronous run discovery |
| AUTO task details | Explain automatic execution and observe scheduler latency | Manual AI launch offered; no polling before first run exists | P1 | READY state excluded from polling; manual launch conditions include AUTO types |
| Publication calendar | Approved unscheduled posts have a visible scheduling route | Only calendar/history; no scheduling candidates | P2 | Data model read on page excludes content without publication |
| Scheduled publication | Exact version/channel/time/campaign and delivery availability | Version omitted; provider_enabled ignored | P2 | Operational metadata not presented |
| Content scheduling recommendation | Exact #publication route opens scheduling | Content page lacked #publication id | P2 | Backend recommendation and frontend target diverged |
| Feedback recommendation | Open feedback disclosure | #feedback target missing | P2 | Disclosure has no id |
| Approvals loading/error | Honest loading state and retry | Empty state while loading; no retry | P2 | No load state or retry action |
| Same-page keyboard CTA / sticky header | Preserve exact hash and show entire target below header | Keyboard Link activation duplicated fragment; initial fixed scroll margin obscured card top | P2 | Same-page Link fragment handling and insufficient header offset; found during browser acceptance |
| Archived plan-item hash | Open containing disclosure and exact target | Other plans collapsed | P2 | No disclosure expansion for direct hash |
| Publication retry/empty states | Recover without full reload; route to content | Retry reloads window; empty state has no content route | P2 | No reusable load action |

Real browser baseline viewport: 1280 x 720. Login opened with saved browser credentials; read-only campaign navigation encountered Failed to fetch, so it is not proof of a successful campaign baseline render. Source inspection confirms the anchor defect independently. No mutating control pressed on the real runtime.

Existing publication read models expose provider_enabled (configuration capability), retry_allowed and reconciliation_required. They do not prove a publishing worker/scheduler is running. UI must show disabled delivery when provider_enabled=false and explicitly unknown runtime readiness otherwise; it must never infer active delivery merely from scheduling. Publishing capability/authorization endpoints remain unchanged.

## Additional confirmed defects and fixes

| Screen/action | Expected | Actual | Severity | Root cause / fix |
|---|---|---|---|---|
| Content after revision approval | Describe exact version as approved | Revision completion banner still asked for approval | P2 | Banner ignored current/approved version IDs; now reflects exact revision version and later editions |
| Campaign with newer approved post | Show existing older scheduled version and replacement route | Existing schedule disappeared and duplicate scheduling was offered | P1 | Lookup required current version match; retain active publication and route to controlled replacement |
| AUTO post task feedback selector | Only show a control that affects the available action | Selector remained without manual run action | P2 | Obsolete manual-run input; only manual campaign planning retains it |
| Completed post progress after approval | Describe task completion independently of current editorial state | Task completion implied post still awaited approval | P2 | Completed run cannot prove current content approval state; link to material for current state |
| Tasks history metric / direct hash | Reveal task history | Fragment reached collapsed disclosure | P2 | Shared async hash hook now opens history and focuses it |

## Before / after operator journey

Before: next-step fragment had no target; create-post attempted manual execution and depended on its run ID; refresh lost progress. Approved unscheduled posts were absent from Publications, and scheduled rows hid their exact version/delivery state.

After: recommendation navigates to a highlighted, focused plan item below the sticky header. Create-post creates one READY Task and reads Task/AgentRun state; refresh discovers the existing task through the workspace read model. Operator reviews the generated material, saves manual edits as separate versions, requests revision, approves an exact version, and follows an explicit scheduling route. Publications shows exact scheduled version ID, campaign, channel, browser-local time and status. External delivery remains separate. Older scheduled versions remain visible and use the existing controlled replacement endpoint.

## Validation

- Frontend: **123 tests passed, 15 files**; ESLint passed without warnings; production build passed; git diff --check passed.
- Regression coverage: real workspace next_step href/target; asynchronous direct hashes and collapsed disclosures; no tasksApi.run on create-post; all four AUTO task types exclude manual run; asynchronous READY/QUEUED/RUNNING/COMPLETED and FAILED/transient errors; approved scheduling candidates; exact operational version/status; cancelled exclusion; undated failures; delivery disabled/unknown; useful empty/error recovery; completed revision approval state; older scheduled-version replacement route; Tasks history.
- Backend production code unchanged: backend regressions not required by this request. Authorization, exact-version validation, worker/provider capability and publication execution contracts remain unchanged.
- Acceptance used a production frontend at localhost:3001 and the checked-in loopback-only in-memory fixture API at localhost:8101. No real database, broker, provider credentials or upstream calls were used by that fixture.
- Desktop **1280×720** (baseline screenshot width): campaign workspace, keyboard Enter on primary CTA, exact hash/focus/highlight, target top 84px below 68px sticky header, no horizontal overflow; refresh while pending; Task queued/run observation; browser back/forward; Content list/review; manual edit; mocked revision; human approval; approved unscheduled Publications CTA; scheduling; exact scheduled-version replacement; Approvals; Knowledge; Publications empty/error/retry; direct strategy/feedback/publication/history anchors.
- Mobile **390×844**: direct plan-item hash, focus and highlight below responsive 105px header, no page horizontal overflow; content approval/replacement controls; scheduled Publications exact post-v3/channel/time/status and disabled delivery. Navigation intentionally scrolls within its own horizontal strip.
- Loading states are covered by automated tests; FAILED/reconciliation capability controls are covered by mocked regression tests. Real delivery/reconciliation/provider actions were not executed.
- Fixture request journal: manual /run requests **0**, provider calls **0**. Mutating acceptance routes were only in-memory fixture task/edit/revision/approval/scheduling/replacement and fixture mode controls.
- Real runtime: read-only docker listing confirms postgres/redis/backend/frontend running; no project AI/publishing/metrics workers or schedulers running. Main remains release **22f869d5a7f77b40e7de4418b8aa5ef0a0ab0cf7**, tracked modifications none, original two untracked DOCX untouched.

## Files and deliberate limits

Frontend pages: campaigns/[id], campaigns/[id]/edit, content/[id], publications, approvals, tasks, tasks/[id]. Shared components: hash-link, use-hash-target, autonomous-post-progress, publication-delivery-notice. Supporting changes: tasks constants, target styles, corresponding frontend regressions and scripts/acceptance/ui_fixture_api.mjs.

No backend read-model field was added: provider_enabled is an existing capability flag, not proof of a running publishing worker. False shows “Автоматическая отправка сейчас отключена”; true/absent explicitly leaves runtime readiness unconfirmed. Live publishing-runtime readiness remains a documented read-model gap. Adding a broker/container probe to this UX fix would require a separate contract and operational design.

Explicit human-authorized retry remains available through the existing retry API; it is not the obsolete automatic create-then-run flow. No worker enablement, deploy, credential rotation, real AI generation or external publication was performed. The real-runtime Failed to fetch baseline issue was not silently treated as successful live acceptance; full mutating acceptance is isolated and cannot prove deployed API connectivity.

**Provider calls in this audit: 0. Real business mutations: 0.**

Final status: **READY FOR UX REVIEW / PROVIDER ACTIONS NOT RUN**.
