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
