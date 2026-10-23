from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "vk_user_token_preflight.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vk_user_token_preflight", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_vk_error_rendering_does_not_expose_request_parameters() -> None:
    script = load_script()
    payload = {
        "error": {
            "error_code": 5,
            "error_msg": "User authorization failed",
            "request_params": [
                {"key": "access_token", "value": "must-never-appear"},
                {"key": "v", "value": "5.199"},
            ],
        }
    }

    rendered = script.safe_vk_error(payload)

    assert rendered == "5: User authorization failed"
    assert "must-never-appear" not in rendered
    assert "access_token" not in rendered


def test_parse_redirect_accepts_query_and_payload_without_printing_secrets() -> None:
    script = load_script()
    payload = json.dumps(
        {
            "code": "one-time-code",
            "device_id": "device-1",
            "state": "expected-state",
        }
    )
    redirect = (
        "https://example.com/callback?state=expected-state"
        f"#payload={script.urllib.parse.quote(payload)}"
    )

    parsed = script.parse_redirect(redirect)

    assert parsed == {
        "state": "expected-state",
        "code": "one-time-code",
        "device_id": "device-1",
    }


def test_read_only_preflight_calls_only_read_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = load_script()
    calls: list[str] = []

    def fake_api_call(method: str, _access_token: str, **_params: Any) -> Any:
        calls.append(method)
        responses: dict[str, Any] = {
            "users.get": [
                {
                    "id": 176199961,
                    "screen_name": "operator",
                    "first_name": "Operator",
                    "last_name": "User",
                }
            ],
            "account.getAppPermissions": script.REQUIRED_PERMISSION_MASK,
            "groups.getById": {
                "groups": [
                    {
                        "id": script.DEFAULT_GROUP_ID,
                        "screen_name": "iteamanagement",
                    }
                ]
            },
            "groups.get": {
                "items": [
                    {
                        "id": script.DEFAULT_GROUP_ID,
                        "screen_name": "iteamanagement",
                        "admin_level": 3,
                    }
                ]
            },
            "wall.get": {"count": 963, "items": []},
        }
        return responses[method]

    monkeypatch.setattr(script, "api_call", fake_api_call)

    result = script.read_only_preflight(
        "secret-access-token",
        group_id=script.DEFAULT_GROUP_ID,
        expected_user_id=176199961,
    )

    assert calls == [
        "users.get",
        "account.getAppPermissions",
        "groups.getById",
        "groups.get",
        "wall.get",
    ]
    assert "wall.post" not in calls
    assert result["wall_count"] == 963
    assert result["managed"]["admin_level"] == 3


def test_required_token_fields_reject_missing_publish_scopes() -> None:
    script = load_script()

    with pytest.raises(script.PreflightError, match="wall и groups"):
        script.required_token_fields(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "user_id": 176199961,
                "scope": "wall",
            }
        )


def test_provisioning_passes_credentials_only_through_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = load_script()
    calls: list[dict[str, Any]] = []
    payload = {
        "schema_version": 1,
        "access_token": "secret-access",
        "refresh_token": "secret-refresh",
        "owner_id": -150574411,
    }

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(script.subprocess, "run", fake_run)

    script.provision_docker_volume(tmp_path, payload)

    assert len(calls) == 2
    combined_arguments = "\n".join(" ".join(call["command"]) for call in calls)
    assert "secret-access" not in combined_arguments
    assert "secret-refresh" not in combined_arguments
    assert calls[0]["input"] == json.dumps(payload, ensure_ascii=False)
    assert calls[1].get("input") is None
    assert calls[0]["stdout"] is subprocess.DEVNULL
    assert calls[0]["stderr"] is subprocess.PIPE
    assert calls[1]["stdout"] is subprocess.DEVNULL
    assert calls[1]["stderr"] is subprocess.PIPE
