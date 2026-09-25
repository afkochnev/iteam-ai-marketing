#!/usr/bin/env bash
set -euo pipefail

base_url="${API_BASE_URL:-http://localhost:8000}"
curl --fail --silent --show-error "$base_url/health" >/dev/null
curl --fail --silent --show-error "$base_url/health/ready" >/dev/null
echo "health and readiness: ok"
