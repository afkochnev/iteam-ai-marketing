#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose --profile publishing ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Health}}'
