# VK automatic publishing

VK publishing uses a **VK ID user credential** for the exact community manager.
It must never reuse `VK_METRICS_ACCESS_TOKEN`: metrics credentials are read-only
and remain isolated in `metrics_worker`.

## Runtime boundaries

- exact target: community `150574411`, owner `-150574411`;
- `wall.post` always uses `from_group=1`;
- target mismatch is rejected before credential access or a network request;
- the refreshable credential file is mounted only into `publication_worker`;
- `backend`, AI workers, publication control/scheduler and `metrics_worker` do
  not receive the credential file;
- access and refresh token values are never stored in PostgreSQL or `.env`;
- an expiring access token is refreshed through VK ID and the rotated refresh
  token is atomically persisted under a cross-process file lock;
- each refresh is validated with read-only `users.get`,
  `account.getAppPermissions` and `wall.get` before the new token is saved.

## Non-secret configuration

Keep publishing disabled while provisioning:

```dotenv
VK_PUBLISHING_ENABLED=false
VK_ACCESS_TOKEN=
VK_CLIENT_ID=54815172
VK_EXPECTED_USER_ID=176199961
VK_OWNER_ID=-150574411
VK_CANARY_PUBLICATION_ID=
VK_API_VERSION=5.199
VK_REQUEST_TIMEOUT_SECONDS=30
VK_TOKEN_REFRESH_LEEWAY_SECONDS=300
```

`VK_ACCESS_TOKEN` is a development/test fallback and must remain empty in
production.

## Provisioning

The operator runs:

```bash
./scripts/vk-publishing-credentials.sh provision
```

The command opens/prints a VK ID authorization URL using PKCE. Paste the full
`http://localhost` callback URL only into the hidden Terminal prompt. Never
paste the callback, authorization code, access token or refresh token into chat
or directly after the shell prompt.

Provisioning performs only read operations:

- `users.get` for exact user `176199961`;
- `account.getAppPermissions` for `wall` and `groups`;
- exact community lookup and administrator/creator role validation;
- `wall.get` for owner `-150574411`.

It does **not** call `wall.post`, write PostgreSQL, edit `.env` or enable the
provider. The credential is stored with mode `0600` in the
`vk_publishing_credentials` Docker volume.

Safe follow-up checks:

```bash
./scripts/vk-publishing-credentials.sh status
./scripts/vk-publishing-credentials.sh refresh
```

`refresh` rotates the token and repeats read-only identity/permission/wall
checks. It still performs zero VK writes.

## Canary gate

Select one exact approved VK `Publication` and arm only that UUID:

```bash
./scripts/vk-publishing-runtime.sh canary <publication-uuid>
```

The script refuses to proceed when:

- the running release does not equal repository `HEAD`;
- credentials are missing or invalid;
- another VK publication is `PUBLISHING`;
- the selected row is not VK, not approved, already failed or already has
  delivery state.

While `VK_CANARY_PUBLICATION_ID` is set, every other VK publication is blocked
both in the dispatcher and again during the durable claim/execution path before
any provider call. The canary can be sent at its due time or explicitly through
`Publish now`.

After verifying the exact post on the community wall, promote to normal
automatic delivery:

```bash
./scripts/vk-publishing-runtime.sh promote
```

Promotion requires the canary to be durably `PUBLISHED`, bound to owner
`-150574411`, with an external post id. It also refuses promotion if any other
VK rows would become immediately due inside the automatic dispatch window.

Emergency disable:

```bash
./scripts/vk-publishing-runtime.sh disable
```

This changes only runtime configuration. It does not alter Publication business
state or execute manual SQL.

## Consolidated activation safeguards

There is one credential store (`app.integrations.vk_credentials`) and one
operator credential command (`vk-publishing-credentials.sh`). No callback or
credential needs to pass through chat. Both authorization and refresh require
an exact echoed state, the expected user, explicit wall/groups scopes and a
nonempty rotating refresh token. Provisioning performs a read-only refresh
exchange before saving credentials. Stored credentials require a regular file,
mode 0600, and the publication worker UID (10001 in Compose); symlinks and
unsupported schemas are rejected. The isolated root provisioning command
validates against that same explicitly configured owner UID.

Production workers reject static tokens for both APP_ENV=production and prod.
The provider also rejects an explicitly supplied static token in production.
Refresh or persistence failure prevents wall.post. Ambiguous publication
outcomes must be reconciled, never blindly retried.

After deploying an accepted main release with VK disabled, perform:

1. Read-only preflight: `./scripts/vk-publishing-credentials.sh preflight`.
2. Provision through the hidden prompt: `./scripts/vk-publishing-credentials.sh provision`.
3. Read-only inventory: `./scripts/vk-publishing-runtime.sh inventory`.
4. Review every VK row, including future SCHEDULED, overdue, APPROVED,
   content version/item/channel and any existing target/delivery/failure state.
5. Select one exact approved Publication UUID and arm the existing canary command.
6. Verify the real post, audit, durable PUBLISHED, exact target -150574411,
   and absence of other sends. Force refresh, restart the worker and verify
   credential status before separately invoking promote.

Inventory requires VK_PUBLISHING_ENABLED=false and runs PostgreSQL queries in
an enforced read-only transaction. Canary runs inventory before enabling its
exact UUID gate. Automatic eligibility and promotion use the configured
PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS, rather than a fixed hour.
Inventory PASS means the read completed with VK disabled and no in-flight VK
publication; the operator must still review the rows before selecting a canary.
No inventory command modifies business state, credentials or provider settings.

The runtime operator first requires successful database/Redis/schema readiness,
then checks BUILD_SHA from application settings inside backend, publication
worker, control worker and scheduler against the clean checkout HEAD. It does
not expect release metadata in the readiness response. A missing or different
SHA in any role blocks the command before activation.

### Explicit early canary

The existing publish-now action can send a future SCHEDULED VK publication early
only when invoked by an authenticated human and VK_CANARY_PUBLICATION_ID equals
that exact UUID. Delivery approval, approved content integrity and the existing
plan snapshot must still pass. The claim writes a durable
PUBLICATION_CANARY_EARLY_REQUESTED audit event bound to the publication, exact
content version, original schedule, target and one-use execution token. The
worker requires the same exact event and active canary gate before an early
provider call. Scheduled time and approved plan remain unchanged. Schedulers,
other UUIDs, ordinary unrestricted VK mode and Telegram retain their existing
time checks. Never promote while a canary is in flight.

### Safe VK failure diagnostics

New `PUBLICATION_FAILED` audit events for VK API errors include `vk_error_code`
and, when supplied, `vk_error_subcode`. Only JSON integers in the range
0..2147483647 are accepted; missing or malformed values are omitted. The existing
`failure_code`, safe failure message, retry policy, and canary gates are unchanged.
For example, VK error 15/subcode 1133 identifies a method permission denial,
while subcode 1134 identifies an application type permission denial (see the
[official VK error schema](https://github.com/VKCOM/vk-api-schema/blob/master/errors.json)).
Do not infer either subcode from the generic `VK_PERMISSION_ERROR` label.

Raw provider messages, request parameters, credentials, and callback URLs are
never copied into this diagnostic metadata. Previously recorded failures cannot
be enriched retroactively: the original VK response was not retained. Installing
this change does not authorize a new publishing attempt or enable VK.

### One-off diagnostic retry of the failed canary

This release adds a deliberately bounded operation, not a general permission-error
retry policy. It authorizes at most one additional provider request for:

- Publication: `1142723f-4c25-4334-8f22-7e3ceb1f55f4`
- Approved content version: `6a4685cd-128b-4a76-ae4b-4595fb8a3238`
- Owner: `-150574411`

Installing the release does not arm or send anything. After a separate human
approval to execute, perform the normal disabled inventory/preflight, then use
`./scripts/vk-publishing-runtime.sh retry-canary 1142723f-4c25-4334-8f22-7e3ceb1f55f4`.
This command only arms the exact runtime gate. It does not edit Publication state
or publish. Other rows remain outside the exact canary gate.

An authenticated human then invokes exactly once:
`POST /api/v1/publications/1142723f-4c25-4334-8f22-7e3ceb1f55f4/canary-retry`.
No request body or credentials belong in chat. The app requires FAILED with
VK_PERMISSION_ERROR, zero prior retries, no external ID/published timestamp,
the expected prior target, delivery approval, an approved content hash, and a
valid plan snapshot. It preserves the version and scheduled/plan times.

The row lock and unique `PUBLICATION_CANARY_RETRY_REQUESTED` audit event consume
the single manual authorization and bind a fresh execution token. The worker
revalidates that snapshot and commits `PUBLICATION_CANARY_RETRY_CONSUMED` before
calling VK. Queue redelivery and concurrent execution cannot send again, including
following a crash. Even definitive auth errors do not trigger the adapter's usual
refresh-and-resend path in this operation. Credential refresh before the single
provider request remains supported. Permission/error subcodes use the safe audit
diagnostics described above. Existing failure history is retained.

After the outcome is checked, use the normal `disable` command; do not promote.
If an HTTP/queue outcome is ambiguous, inspect durable state without resubmitting.
A crash after the consumed marker may leave delivery uncertain and must follow
normal reconciliation; there is no automatic recovery send. Neither `/retry` nor
`/publish-now` can reuse this authorization, even after reconciliation.

### Empty static-token configuration

Compose intentionally supplies `VK_ACCESS_TOKEN=""`. The adapter treats missing,
empty and whitespace-only static-token values as absent and selects the isolated
refreshable credential store. A nonempty static token remains forbidden in
production. Missing credentials or an empty token returned by a store fail before
`wall.post`. Expired credentials must refresh and persist successfully before the
adapter may use the refreshed token.

This fix does not reset a consumed canary retry, clear its audit, change publishing
gates, or authorize another publication attempt. Keep VK disabled during rollout
and validate refresh separately from any future publishing approval.

### Distinct authorization after the credential-store fix

A second, separately audited one-use authorization is available only for the same
Publication/version/owner listed above, following the failed first retry. It does
not reset, rename or delete the first request, consumed marker or failure audit.
The authenticated endpoint is `POST /api/v1/publications/{id}/canary-retry-v2`;
the operator command is `retry-canary-v2 {id}`. Installation alone sends nothing
and does not change the runtime gates. Human approval to execute is separate from
approval to prepare or deploy this change.

The app requires retry_count=1, the intact first intent and consumed snapshot,
the prior audited VK error 15, current FAILED/VK_PERMISSION_ERROR, no delivery
evidence, exact version/target, the active exact gate, approval hash and valid plan.
A new claim advances retry_count to 2 and binds a new execution token to
`PUBLICATION_CANARY_RETRY_V2_REQUESTED`, with operation key
`publication-canary-retry-v2:{id}` and authorization ID `credential-store-fixed-v1`.
The worker commits its separate `PUBLICATION_CANARY_RETRY_V2_CONSUMED` marker
before the single provider request. The first audit stays immutable.

Old queue messages, old endpoints, duplicate requests, concurrent redelivery,
auth errors, rate limits and uncertain outcomes cannot reuse either consumed
authorization. After the inspected outcome, disable VK without promote. A crash
after consumption requires reconciliation, never a blind resubmission. Any further
permission would require another explicit code change and human approval.
