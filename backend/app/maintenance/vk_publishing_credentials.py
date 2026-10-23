"""Interactive, operator-invoked VK ID credential provisioning.

The command never calls wall.post, writes PostgreSQL, changes .env, or enables a
provider. Callback URLs and token values are read through hidden prompts and are
never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import secrets
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.core.config import settings
from app.integrations.vk_credentials import (
    VK_API_BASE_URL,
    VK_ID_TOKEN_URL,
    VK_REQUIRED_PERMISSIONS,
    VK_REQUIRED_SCOPE,
    VKCredentialError,
    VKCredentialSnapshot,
    VKCredentialStore,
)

REDIRECT_URI = "http://localhost"
ALLOWED_MANAGER_ROLES = {"creator", "administrator"}


class ProvisioningError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.safe_message = message


def _store() -> VKCredentialStore:
    required = {
        "VK_CREDENTIALS_FILE": settings.vk_credentials_file,
        "VK_CLIENT_ID": settings.vk_client_id,
        "VK_EXPECTED_USER_ID": settings.vk_expected_user_id,
        "VK_OWNER_ID": settings.vk_owner_id,
    }
    missing = [name for name, value in required.items() if value in {None, ""}]
    if missing:
        raise ProvisioningError("configuration", "Missing configuration: " + ", ".join(missing))
    assert settings.vk_credentials_file is not None
    assert settings.vk_client_id is not None
    assert settings.vk_expected_user_id is not None
    assert settings.vk_owner_id is not None
    if settings.vk_owner_id >= 0:
        raise ProvisioningError("configuration", "VK_OWNER_ID must be a negative community owner.")
    return VKCredentialStore(
        settings.vk_credentials_file,
        client_id=settings.vk_client_id,
        expected_user_id=settings.vk_expected_user_id,
        owner_id=settings.vk_owner_id,
        api_version=settings.vk_api_version,
        request_timeout_seconds=settings.vk_request_timeout_seconds,
        refresh_leeway_seconds=settings.vk_token_refresh_leeway_seconds,
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _callback_values(callback_url: str, expected_state: str) -> tuple[str, str]:
    parsed = urlparse(callback_url.strip())
    values = parse_qs(parsed.query)
    if not values and parsed.fragment:
        values = parse_qs(parsed.fragment)
    state = (values.get("state") or [""])[0]
    code = (values.get("code") or [""])[0]
    device_id = (values.get("device_id") or [""])[0]
    if state != expected_state:
        raise ProvisioningError("callback", "OAuth state mismatch.")
    if not code or not device_id:
        raise ProvisioningError("callback", "Callback URL lacks code or device_id.")
    return code, device_id


def _json_payload(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise ProvisioningError(stage, "VK returned an invalid response.") from error
    if not isinstance(payload, dict):
        raise ProvisioningError(stage, "VK returned an invalid response.")
    return payload


async def _vk_method(
    client: httpx.AsyncClient,
    method: str,
    access_token: str,
    **parameters: Any,
) -> Any:
    response = await client.post(
        f"{VK_API_BASE_URL}/{method}",
        data={
            **parameters,
            "access_token": access_token,
            "v": settings.vk_api_version,
        },
    )
    payload = _json_payload(response, method)
    error = payload.get("error")
    if response.status_code >= 400 or error:
        code = int(error.get("error_code", 0)) if isinstance(error, dict) else 0
        raise ProvisioningError(method, f"VK read-only method failed with code {code}.")
    if "response" not in payload:
        raise ProvisioningError(method, "VK read-only method returned no response.")
    return payload["response"]


def _groups(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        raw = response.get("groups", response.get("items", []))
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


async def _authorize() -> tuple[VKCredentialSnapshot, dict[str, Any]]:
    store = _store()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    authorize_url = "https://id.vk.ru/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": str(store.client_id),
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": VK_REQUIRED_SCOPE,
        }
    )
    print("Откройте URL в браузере и подтвердите доступы wall и groups:")
    print(authorize_url)
    webbrowser.open(authorize_url)
    callback = getpass.getpass(
        "Вставьте ПОЛНЫЙ callback URL http://localhost (ввод скрыт; не сохраняется): "
    )
    code, device_id = _callback_values(callback, state)
    try:
        async with httpx.AsyncClient(timeout=settings.vk_request_timeout_seconds) as client:
            token_response = await client.post(
                VK_ID_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": str(store.client_id),
                    "redirect_uri": REDIRECT_URI,
                    "code": code,
                    "code_verifier": verifier,
                    "device_id": device_id,
                },
            )
            token_payload = _json_payload(token_response, "token_exchange")
            if token_response.status_code >= 400 or token_payload.get("error"):
                raise ProvisioningError("token_exchange", "VK rejected the authorization code.")
            access_token = str(token_payload.get("access_token") or "")
            refresh_token = str(token_payload.get("refresh_token") or "")
            expires_in = int(token_payload.get("expires_in") or 0)
            if not access_token or not refresh_token or expires_in <= 0:
                raise ProvisioningError(
                    "token_exchange", "VK did not return refreshable user credentials."
                )

            users = await _vk_method(client, "users.get", access_token)
            if not isinstance(users, list) or not users or not isinstance(users[0], dict):
                raise ProvisioningError("identity", "VK user identity was not returned.")
            user = users[0]
            user_id = int(user["id"])
            if user_id != store.expected_user_id:
                raise ProvisioningError(
                    "identity",
                    "Authorized VK user does not match expected user.",
                )

            permissions_mask = int(
                await _vk_method(client, "account.getAppPermissions", access_token)
            )
            if permissions_mask & VK_REQUIRED_PERMISSIONS != VK_REQUIRED_PERMISSIONS:
                raise ProvisioningError("permissions", "VK token lacks wall/groups access.")

            group_id = abs(store.owner_id)
            group_response = await _vk_method(
                client, "groups.getById", access_token, group_ids=str(group_id)
            )
            groups = _groups(group_response)
            if not groups or int(groups[0].get("id", 0)) != group_id:
                raise ProvisioningError("community", "Exact VK community was not returned.")
            community = groups[0]

            admin_response = await _vk_method(
                client,
                "groups.get",
                access_token,
                filter="admin",
                extended="1",
                count="1000",
            )
            admin_groups = _groups(admin_response)
            admin_group = next(
                (item for item in admin_groups if int(item.get("id", 0)) == group_id),
                None,
            )
            admin_level = int(admin_group.get("admin_level", 0)) if admin_group else 0
            if admin_level < 3:
                raise ProvisioningError("community_access", "VK user is not community admin.")

            managers = await _vk_method(
                client,
                "groups.getMembers",
                access_token,
                group_id=str(group_id),
                filter="managers",
                count="1000",
            )
            manager_items = managers.get("items", []) if isinstance(managers, dict) else []
            manager = next(
                (
                    item
                    for item in manager_items
                    if isinstance(item, dict) and int(item.get("id", 0)) == user_id
                ),
                None,
            )
            role = str(manager.get("role", "")) if manager else ""
            if role not in ALLOWED_MANAGER_ROLES:
                raise ProvisioningError(
                    "community_access", "VK user lacks creator/administrator role."
                )

            wall = await _vk_method(
                client,
                "wall.get",
                access_token,
                owner_id=str(store.owner_id),
                count="1",
            )
            if not isinstance(wall, dict) or "count" not in wall:
                raise ProvisioningError("wall_read", "VK community wall was not readable.")
            post_count = int(wall["count"])
    except ProvisioningError:
        raise
    except httpx.TimeoutException as error:
        raise ProvisioningError("network", "VK request timed out.") from error
    except httpx.HTTPError as error:
        raise ProvisioningError("network", "VK request failed.") from error
    except (KeyError, TypeError, ValueError) as error:
        raise ProvisioningError("validation", "VK response could not be validated.") from error

    now = datetime.now(UTC)
    snapshot = VKCredentialSnapshot(
        schema_version=1,
        client_id=store.client_id,
        user_id=user_id,
        owner_id=store.owner_id,
        scope=str(token_payload.get("scope") or VK_REQUIRED_SCOPE),
        permissions_mask=permissions_mask,
        access_token=access_token,
        refresh_token=refresh_token,
        device_id=device_id,
        expires_at=(now + timedelta(seconds=expires_in)).isoformat(),
        updated_at=now.isoformat(),
    )
    safe = {
        "user_id": user_id,
        "user_name": " ".join(
            part
            for part in (
                str(user.get("first_name", "")),
                str(user.get("last_name", "")),
            )
            if part
        ),
        "permissions_mask": permissions_mask,
        "community_id": group_id,
        "owner_id": store.owner_id,
        "community_name": str(community.get("name", "")),
        "screen_name": str(community.get("screen_name", "")),
        "role": role,
        "admin_level": admin_level,
        "post_count": post_count,
        "expires_in": expires_in,
    }
    return snapshot, safe


def _print_pass(safe: dict[str, Any], *, provisioned: bool) -> None:
    print("\nVK USER TOKEN PREFLIGHT: PASS")
    print(f"- User: {safe['user_id']} / {safe['user_name']}")
    print(f"- Permissions: wall=PASS, groups=PASS, mask={safe['permissions_mask']}")
    print(
        "- Community: "
        f"{safe['community_id']} / owner {safe['owner_id']} / @{safe['screen_name']}"
    )
    print(f"- Community role: {safe['role']}; admin_level={safe['admin_level']}")
    print(f"- wall.get: PASS; reported post count={safe['post_count']}")
    print(f"- expires_in: {safe['expires_in']}")
    print("- refresh_token returned: YES")
    print("- Token values were not printed")
    print("- wall.post / VK writes / DB writes / OpenAI calls: 0")
    print("- VK_PUBLISHING_ENABLED remains false")
    print(f"- Credential provisioned: {'YES' if provisioned else 'NO'}")


async def _run(command: str) -> int:
    store = _store()
    if command in {"preflight", "provision"}:
        snapshot, safe = await _authorize()
        provisioned = command == "provision"
        if provisioned:
            owner_uid = int(__import__("os").environ.get("VK_CREDENTIALS_OWNER_UID", "10001"))
            store.provision(snapshot, owner_uid=owner_uid)
        _print_pass(safe, provisioned=provisioned)
        return 0
    if command == "status":
        status = store.safe_status()
        print("VK PUBLISHING CREDENTIAL STATUS: PASS")
        print(f"- client_id: {status['client_id']}")
        print(f"- user_id: {status['user_id']}")
        print(f"- owner_id: {status['owner_id']}")
        print(f"- permissions_mask: {status['permissions_mask']}")
        print(f"- expires_at: {status['expires_at']}")
        print(f"- expired: {'YES' if status['expired'] else 'NO'}")
        print("- refresh_token present: YES")
        print("- Token values were not printed")
        return 0
    if command == "refresh":
        await store.access_token(force_refresh=True)
        status = store.safe_status()
        print("VK PUBLISHING CREDENTIAL REFRESH: PASS")
        print(f"- user_id: {status['user_id']}")
        print(f"- owner_id: {status['owner_id']}")
        print(f"- expires_at: {status['expires_at']}")
        print("- users.get / account.getAppPermissions / wall.get: PASS")
        print("- wall.post / VK writes / DB writes / OpenAI calls: 0")
        print("- Token values were not printed")
        return 0
    raise ProvisioningError("command", "Unsupported command.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("preflight", "provision", "status", "refresh"),
        nargs="?",
        default="status",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args.command)))
    except (ProvisioningError, VKCredentialError) as error:
        stage = error.stage if isinstance(error, ProvisioningError) else "credentials"
        message = error.safe_message
        print("\nVK USER TOKEN PREFLIGHT: FAIL", file=sys.stderr)
        print(f"- Stage: {stage}", file=sys.stderr)
        print(f"- Reason: {message}", file=sys.stderr)
        print("- wall.post / VK writes / DB writes / config changes: 0", file=sys.stderr)
        print("- VK_PUBLISHING_ENABLED: unchanged; keep false", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
