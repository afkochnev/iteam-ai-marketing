# VK community publishing

VK publishing uses a **VK ID user authorization** belonging to a person who can
publish on behalf of the target community. The read-only metrics service token
is a separate credential and must never be used by the publication worker.

## Credential lifecycle

VK ID access tokens are short-lived. The authorization response also contains a
refresh token and `device_id`. Production stores the rotating credential state
in `.runtime-secrets/vk/credentials.json` with mode `0600`; the directory is
mounted only into `publication_worker`.

Before `wall.post`, the worker:

1. checks that the requested target exactly equals negative `VK_OWNER_ID`;
2. reads the target-bound credential file under an exclusive lock;
3. refreshes an expiring token through `https://id.vk.ru/oauth2/auth`;
4. validates response `state`, user identity, scopes, and target binding;
5. atomically persists the rotated access and refresh tokens;
6. only then calls `wall.post` with `from_group=1`.

If refresh or persistence fails, no wall request is made.

## Read-only authorization and preflight

Keep `VK_PUBLISHING_ENABLED=false`. Run from the repository root:

```bash
python3 scripts/vk_user_token_preflight.py --provision
```

The script asks for the VK ID application ID and the exact registered redirect
URI, opens the VK ID consent page with scopes `wall groups`, and accepts the
redirect URL through a hidden prompt. It then performs only:

- `users.get`;
- `account.getAppPermissions`;
- `groups.getById`;
- `groups.get` with the admin filter;
- `wall.get`;
- one VK ID refresh-token exchange.

It never calls `wall.post`, never changes PostgreSQL, and never prints token
values. Credentials are written only after the full preflight and refresh
lifecycle pass.

## Activation gates

Do not enable VK immediately after provisioning. First inspect PostgreSQL
read-only and resolve every existing VK row that could be dispatched. Then:

1. keep the publication scheduler stopped;
2. enable VK only for an explicitly selected, human-approved canary;
3. execute the canary and verify the exact community owner and returned post id;
4. reconcile any ambiguous result before retrying;
5. start automatic dispatch only after the canary passes.

`VK_OWNER_ID` must be the negative community owner id. For ITeam Management the
expected value is `-150574411`.
