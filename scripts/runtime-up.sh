#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-normal}"
case "$mode" in
  normal)
    exec docker compose --profile publishing up -d postgres redis backend frontend ai_worker ai_control_worker ai_scheduler publication_worker publication_control_worker publication_scheduler metrics_worker
    ;;
  maintenance)
    docker compose --profile publishing stop ai_worker ai_control_worker ai_scheduler publication_worker publication_control_worker publication_scheduler metrics_worker
    exec docker compose up -d postgres redis backend frontend
    ;;
  *) echo 'Usage: runtime-up.sh [normal|maintenance]' >&2; exit 2 ;;
esac
