# Optimization proposals and human decisions

An accepted FeedbackAnalysis can materialize one advisory proposal. PostgreSQL enforces uniqueness of the source analysis, action position and recommendation index. Acceptance locks the analysis and commits its review, the proposal, all actions and the activity event together. No task or AgentRun is created at acceptance.

New FeedbackAnalystResult recommendations require a typed `proposed_action`. The existing feedback worker validates both evidence and targets and retains its existing single repair attempt. Proposal materialization never calls a model. Legacy persisted recommendations remain readable; accepting an analysis without typed actions creates no proposal and does not infer targets or synthesize NO_CHANGE.

The analysis snapshot freezes:

- Campaign ID and exact strategy version.
- Published content item/version pairs verified through the content ownership relation.
- Campaign publication plans, status, time horizon, update time and ordered source item/version identities.
- The existing publication/version/metrics/feedback evidence allowlists.

Completion and acceptance validate against this frozen context, not the campaign's current strategy or current content version. The action type determines its permitted target entity type and version requirements. Polymorphic target IDs are never arbitrary model URLs. UI evidence is presented as validated typed identifiers; no model-provided href is rendered.

Authenticated endpoints:

- GET `/campaigns/{campaign_id}/optimization-proposals`
- GET `/optimization-proposals/{proposal_id}`
- POST `/optimization-actions/{action_id}/approve`
- POST `/optimization-actions/{action_id}/reject`

Action decisions lock the campaign, proposal and action in that order. Locking the proposal serializes sibling decisions and aggregation as well as opposite decisions on one action. Repeating the same decision is idempotent; the opposite decision returns 409. Archived campaigns remain readable and decisions return 409. The final human decision records proposal reviewer/time. Events contain only provenance identifiers and typed status, with the user in ActivityLog.user_id.

The UI shows a separate card per action, with independent decisions and an explicit advisory notice. Decisions are refreshed from the API and persist across reload. No bulk decision or Apply control exists.

PR26 intentionally has no application workflow. Approval creates zero Tasks, AgentRuns, ContentVersions, PublicationPlans or Publications. APPLIED/FAILED/SUPERSEDED enum values are reserved and cannot be assigned through these routes. Director Chat, executable tools, publication execution and worker queue topology are unchanged.

Migration `20261007_0023` creates only the proposal/action tables, constraints, indexes and four enums; it does not backfill legacy analysis snapshots. Downgrade drops only these tables and enums. Migration regression runs only against an isolated test database. Tests block real OpenAI, Telegram and VK DNS and mock structured model output.
