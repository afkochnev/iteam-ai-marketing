#!/usr/bin/env python3
"""Authorize a VK ID user, verify read-only publishing access, and optionally provision.

Secrets are accepted only through hidden interactive prompts or the VK redirect URL.
The script never calls wall.post and never prints tokens.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

VK_ID_AUTHORIZE = "https://id.vk.ru/authorize"
VK_ID_TOKEN = "https://id.vk.ru/oauth2/auth"
VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.199"
WALL_PERMISSION = 8192
GROUPS_PERMISSION = 262144
REQUIRED_PERMISSION_MASK = WALL_PERMISSION | GROUPS_PERMISSION
DEFAULT_GROUP_ID = 150574411


class PreflightError(RuntimeError):
    pass


def safe_vk_error(payload: dict[str, Any]) -> str:
    error = payload.get("error", "UNKNOWN_ERROR")
    if isinstance(error, dict):
        code = error.get("error_code", "UNKNOWN_ERROR")
        message = error.get("error_msg", "VK API error")
        return f"{code}: {message}"
    description = payload.get("error_description", "")
    return f"{error} {description}".strip()


def hidden(prompt: str) -> str:
    value = getpass.getpass(prompt).strip()
    if not value:
        raise PreflightError("Обязательное секретное значение не введено.")
    return value


def post_form(
    url: str, *, query: dict[str, Any], form: dict[str, Any]
) -> dict[str, Any]:
    request_url = f"{url}?{urllib.parse.urlencode(query)}"
    encoded = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        request_url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read())
            message = (
                safe_vk_error(payload) if isinstance(payload, dict) else "HTTP_ERROR"
            )
        except (ValueError, AttributeError):
            message = "HTTP_ERROR"
        raise PreflightError(f"VK отклонил запрос: {message}") from exc
    except urllib.error.URLError as exc:
        raise PreflightError(
            "Не удалось подключиться к VK. Проверьте сеть и повторите."
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise PreflightError("VK вернул ответ в неизвестном формате.") from exc
    if not isinstance(payload, dict):
        raise PreflightError("VK вернул ответ в неизвестном формате.")
    return payload


def api_call(method: str, access_token: str, **params: Any) -> Any:
    payload = post_form(
        f"{VK_API}/{method}",
        query={},
        form={
            **params,
            "access_token": access_token,
            "v": VK_API_VERSION,
        },
    )
    if "error" in payload:
        error = payload["error"]
        code = error.get("error_code", "?") if isinstance(error, dict) else "?"
        message = (
            error.get("error_msg", "VK API error")
            if isinstance(error, dict)
            else "VK API error"
        )
        raise PreflightError(f"{method}: VK API error {code}: {message}")
    if "response" not in payload:
        raise PreflightError(f"{method}: VK API не вернул response.")
    return payload["response"]


def parse_redirect(value: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(value)
    combined: dict[str, list[str]] = {}
    for source in (parsed.query, parsed.fragment):
        for key, items in urllib.parse.parse_qs(source).items():
            combined.setdefault(key, []).extend(items)
    if "payload" in combined:
        try:
            payload = json.loads(combined["payload"][0])
        except (ValueError, TypeError) as exc:
            raise PreflightError(
                "Не удалось разобрать payload из redirect URL."
            ) from exc
        if isinstance(payload, dict):
            for key, item in payload.items():
                combined.setdefault(str(key), []).append(str(item))
    return {key: values[0] for key, values in combined.items() if values}


def scope_allows_publishing(scope: str) -> bool:
    normalized = scope.replace(",", " ").strip()
    if normalized.isdigit():
        return int(normalized) & REQUIRED_PERMISSION_MASK == REQUIRED_PERMISSION_MASK
    return {"wall", "groups"}.issubset(set(normalized.lower().split()))


def exchange_code(
    *,
    client_id: int,
    redirect_uri: str,
    code: str,
    device_id: str,
    code_verifier: str,
    state: str,
) -> dict[str, Any]:
    payload = post_form(
        VK_ID_TOKEN,
        query={
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "state": state,
            "device_id": device_id,
        },
        form={"code": code},
    )
    if "error" in payload:
        raise PreflightError(f"VK ID отклонил код: {safe_vk_error(payload)}")
    if str(payload.get("state")) != state:
        raise PreflightError("VK ID вернул другой state при обмене кода.")
    return payload


def refresh_token(
    *,
    client_id: int,
    redirect_uri: str,
    device_id: str,
    refresh_token_value: str,
    expected_user_id: int,
) -> dict[str, Any]:
    state = secrets.token_urlsafe(32)
    payload = post_form(
        VK_ID_TOKEN,
        query={
            "grant_type": "refresh_token",
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "device_id": device_id,
            "state": state,
        },
        form={"refresh_token": refresh_token_value},
    )
    if "error" in payload:
        raise PreflightError(f"VK ID отклонил refresh token: {safe_vk_error(payload)}")
    if str(payload.get("state")) != state:
        raise PreflightError("VK ID вернул другой state при обновлении токена.")
    if int(payload.get("user_id", 0)) != expected_user_id:
        raise PreflightError("После refresh изменилась identity пользователя.")
    return payload


def normalize_group_list(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        candidates = response.get("groups", response.get("items", []))
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
    return []


def read_only_preflight(
    access_token: str, *, group_id: int, expected_user_id: int
) -> dict[str, Any]:
    users = api_call("users.get", access_token, fields="screen_name")
    if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
        raise PreflightError("users.get не подтвердил единственного пользователя.")
    user = users[0]
    user_id = int(user.get("id", 0))
    if user_id != expected_user_id:
        raise PreflightError("users.get вернул другого пользователя.")

    permissions = int(api_call("account.getAppPermissions", access_token))
    if permissions & REQUIRED_PERMISSION_MASK != REQUIRED_PERMISSION_MASK:
        raise PreflightError(
            "Токен не содержит одновременно permissions wall и groups. "
            f"mask={permissions}"
        )

    group_details = normalize_group_list(
        api_call(
            "groups.getById",
            access_token,
            group_ids=str(group_id),
            fields="screen_name",
        )
    )
    target = next(
        (item for item in group_details if int(item.get("id", 0)) == group_id), None
    )
    if target is None:
        raise PreflightError("groups.getById не подтвердил целевое сообщество.")

    administered = api_call(
        "groups.get",
        access_token,
        filter="admin",
        extended=1,
        count=1000,
        fields="screen_name,admin_level,is_admin",
    )
    admin_groups = normalize_group_list(administered)
    managed = next(
        (item for item in admin_groups if int(item.get("id", 0)) == group_id), None
    )
    if managed is None:
        raise PreflightError("Пользователь не управляет целевым сообществом.")

    wall = api_call("wall.get", access_token, owner_id=-group_id, count=1)
    if not isinstance(wall, dict) or "count" not in wall:
        raise PreflightError("wall.get не подтвердил доступ к стене сообщества.")

    return {
        "user": user,
        "permissions": permissions,
        "group": target,
        "managed": managed,
        "wall_count": int(wall["count"]),
    }


def provision_docker_volume(repository_dir: Path, payload: dict[str, Any]) -> None:
    writer = r"""
import json
import os
import sys
import tempfile
from pathlib import Path

target = Path("/run/iteam-vk")
credentials = target / "credentials.json"
data = sys.stdin.read()
parsed = json.loads(data)
if not isinstance(parsed, dict):
    raise SystemExit("invalid credential payload")
target.mkdir(parents=True, exist_ok=True)
os.chown(target, 10001, 10001)
os.chmod(target, 0o700)
descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials.", dir=target)
temporary = Path(temporary_name)
try:
    os.fchown(descriptor, 10001, 10001)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(parsed, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, credentials)
    os.chown(credentials, 10001, 10001)
    os.chmod(credentials, 0o600)
finally:
    if temporary.exists():
        temporary.unlink()
"""
    verifier = r"""
import json
import os
import stat
from pathlib import Path

path = Path("/run/iteam-vk/credentials.json")
payload = json.loads(path.read_text(encoding="utf-8"))
metadata = path.stat()
mode = stat.S_IMODE(metadata.st_mode)
if mode != 0o600 or metadata.st_uid != 10001 or os.geteuid() != 10001:
    raise SystemExit("credential ownership or mode check failed")
if int(payload.get("owner_id", 0)) >= 0 or not payload.get("refresh_token"):
    raise SystemExit("credential payload verification failed")
with path.open("a", encoding="utf-8"):
    pass
"""
    base = [
        "docker",
        "compose",
        "run",
        "--rm",
        "-T",
        "--no-deps",
    ]
    try:
        subprocess.run(
            [
                *base,
                "--user",
                "0:0",
                "--entrypoint",
                "python",
                "publication_worker",
                "-c",
                writer,
            ],
            cwd=repository_dir,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            [
                *base,
                "--user",
                "10001:10001",
                "--entrypoint",
                "python",
                "publication_worker",
                "-c",
                verifier,
            ],
            cwd=repository_dir,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(
            "Не удалось сохранить VK credentials в изолированный Docker volume."
        ) from exc


def required_token_fields(payload: dict[str, Any]) -> tuple[str, str, int, int, str]:
    try:
        access_token = str(payload["access_token"])
        refresh_token_value = str(payload["refresh_token"])
        expires_in = int(payload["expires_in"])
        user_id = int(payload["user_id"])
        scope = str(payload["scope"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError("VK ID вернул неполный token response.") from exc
    if not access_token or not refresh_token_value or expires_in <= 0 or user_id <= 0:
        raise PreflightError("VK ID вернул непригодные credentials.")
    if not scope_allows_publishing(scope):
        raise PreflightError("Token response не содержит scopes wall и groups.")
    return access_token, refresh_token_value, expires_in, user_id, scope


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--redirect-uri")
    parser.add_argument("--group-id", type=int, default=DEFAULT_GROUP_ID)
    parser.add_argument("--repository-dir", type=Path, default=Path.cwd())
    parser.add_argument("--provision", action="store_true")
    args = parser.parse_args()

    try:
        client_id = args.client_id or int(input("VK ID application ID: ").strip())
        redirect_uri = (
            args.redirect_uri
            or input("Точный redirect URI из кабинета VK ID: ").strip()
        )
        if client_id <= 0 or not redirect_uri:
            raise PreflightError("Application ID или redirect URI не заданы.")
        group_id = args.group_id
        if group_id <= 0:
            raise PreflightError("Group ID должен быть положительным.")

        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        state = secrets.token_urlsafe(32)
        authorize_url = f"{VK_ID_AUTHORIZE}?{
            urllib.parse.urlencode(
                {
                    'response_type': 'code',
                    'client_id': client_id,
                    'redirect_uri': redirect_uri,
                    'scope': 'wall groups',
                    'state': state,
                    'code_challenge': code_challenge,
                    'code_challenge_method': 'S256',
                    'prompt': 'consent',
                }
            )
        }"

        print("\nОткройте URL авторизации VK ID. Он не содержит access token:")
        print(authorize_url)
        webbrowser.open(authorize_url)
        redirect_value = hidden(
            "После согласия вставьте сюда полный redirect URL (ввод скрыт): "
        )
        redirect_data = parse_redirect(redirect_value)
        if redirect_data.get("state") != state:
            raise PreflightError("State в redirect URL не совпадает.")
        code = redirect_data.get("code", "")
        device_id = redirect_data.get("device_id", "")
        if not code or not device_id:
            raise PreflightError("Redirect URL не содержит code и device_id.")

        issued = exchange_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code=code,
            device_id=device_id,
            code_verifier=code_verifier,
            state=state,
        )
        access_token, refresh_value, _expires, user_id, _scope = required_token_fields(
            issued
        )
        read_only_preflight(access_token, group_id=group_id, expected_user_id=user_id)

        refreshed = refresh_token(
            client_id=client_id,
            redirect_uri=redirect_uri,
            device_id=device_id,
            refresh_token_value=refresh_value,
            expected_user_id=user_id,
        )
        access_token, refresh_value, expires_in, user_id, scope = required_token_fields(
            refreshed
        )
        second = read_only_preflight(
            access_token, group_id=group_id, expected_user_id=user_id
        )

        user = second["user"]
        group = second["group"]
        managed = second["managed"]
        print("\nVK USER TOKEN PREFLIGHT: PASS")
        print(
            f"- User: {user_id} / @{user.get('screen_name', f'id{user_id}')} / "
            f"{user.get('first_name', '')} {user.get('last_name', '')}".rstrip()
        )
        print(f"- Permissions wall/groups: PASS; mask={second['permissions']}")
        print(
            f"- Community: {group_id} / owner {-group_id} / "
            f"@{group.get('screen_name', '')}"
        )
        print(
            f"- Community management: PASS; admin_level={managed.get('admin_level', 'confirmed')}"
        )
        print(f"- users.get / wall.get: PASS; wall_count={second['wall_count']}")
        print(f"- Refresh lifecycle: PASS; expires_in={expires_in}")
        print("- wall.post / other VK writes / DB writes / OpenAI calls: 0")
        print("- Token values printed: 0")

        if args.provision:
            credentials = {
                "schema_version": 1,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "device_id": device_id,
                "user_id": user_id,
                "owner_id": -group_id,
                "scope": scope,
                "access_token": access_token,
                "refresh_token": refresh_value,
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=expires_in)
                ).isoformat(),
            }
            provision_docker_volume(args.repository_dir.resolve(), credentials)
            print("- Credentials provisioned: isolated Docker volume")
            print("- Runtime owner/mode: uid=10001 / 0600")
        else:
            print("- Credentials provisioned: NO; tokens discarded")
        return 0
    except (PreflightError, ValueError) as exc:
        print("\nVK USER TOKEN PREFLIGHT: FAIL", file=sys.stderr)
        print(f"- {exc}", file=sys.stderr)
        print(
            "- wall.post / other VK writes / DB writes / OpenAI calls: 0",
            file=sys.stderr,
        )
        print("- Token values printed: 0", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
