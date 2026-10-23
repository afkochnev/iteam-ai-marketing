#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

command_name="${1:-status}"
case "$command_name" in
  preflight|provision|status|refresh) ;;
  *)
    echo "Usage: $0 {preflight|provision|status|refresh}" >&2
    exit 2
    ;;
esac

if [[ "$command_name" == "provision" ]]; then
  enabled="$(awk -F= '$1 == "VK_PUBLISHING_ENABLED" {print tolower($2)}' .env | tail -1 | tr -d '[:space:]\"')"
  if [[ "$enabled" != "false" && "$enabled" != "0" && -n "$enabled" ]]; then
    echo "VK_PUBLISHING_ENABLED must be false before credential provisioning." >&2
    exit 1
  fi
fi

docker compose --profile vk-admin run --rm --no-deps vk_credentials_admin "$command_name"
