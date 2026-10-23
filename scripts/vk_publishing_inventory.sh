#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${ITEAM_REPO_DIR:-$HOME/iteam-ai-marketing}"
cd "$REPO_DIR"

vk_enabled="$(
  awk -F= '$1 == "VK_PUBLISHING_ENABLED" { value=$2 } END { print value }' .env |
    tr -d '[:space:]"' |
    tr '[:upper:]' '[:lower:]'
)"

if [[ "$vk_enabled" != "false" ]]; then
  printf 'INVENTORY: FAIL\n- VK_PUBLISHING_ENABLED must remain false during inventory\n' >&2
  exit 1
fi

max_lateness_seconds="$(
  awk -F= '
    $1 == "PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS" {
      value=$2
    }
    END { print value }
  ' .env | tr -d '[:space:]"'
)"
max_lateness_seconds="${max_lateness_seconds:-3600}"
if [[ ! "$max_lateness_seconds" =~ ^[1-9][0-9]*$ ]]; then
  printf 'INVENTORY: FAIL\n- Invalid PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS\n' >&2
  exit 1
fi

PGUSER="$(
  docker compose exec -T postgres sh -lc 'printf %s "$POSTGRES_USER"' |
    tr -d '\r\n'
)"
PGDB="$(
  docker compose exec -T postgres sh -lc 'printf %s "$POSTGRES_DB"' |
    tr -d '\r\n'
)"

[[ -n "$PGUSER" && -n "$PGDB" ]]

printf '=== VK PUBLICATION COUNTS ===\n'
docker compose exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 \
  -v max_lateness_seconds="$max_lateness_seconds" \
  -U "$PGUSER" -d "$PGDB" -P pager=off -c "
WITH parameters AS (
  SELECT make_interval(secs => :'max_lateness_seconds'::integer) AS dispatch_window
)
SELECT
  count(*) FILTER (WHERE p.status = 'PUBLISHING') AS publishing,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.approved_for_publish_at IS NOT NULL
      AND p.approved_for_publish_by IS NOT NULL
      AND p.execution_token IS NULL
      AND p.failure_code IS NULL
      AND p.scheduled_at <= now()
      AND p.scheduled_at >= now() - parameters.dispatch_window
  ) AS auto_dispatch_eligible_now,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.scheduled_at < now() - parameters.dispatch_window
  ) AS overdue_outside_auto_window,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.scheduled_at > now()
  ) AS future_scheduled,
  count(*) FILTER (
    WHERE p.status IN ('DRAFT', 'WAITING_APPROVAL', 'APPROVED', 'FAILED')
  ) AS other_nonterminal_or_attention,
  count(*) FILTER (
    WHERE p.status IN ('PUBLISHED', 'CANCELLED')
  ) AS terminal_history
FROM publications p
CROSS JOIN parameters
WHERE p.channel::text = 'VK';
"

printf '\n=== VK PUBLICATION INVENTORY ===\n'
docker compose exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 \
  -v max_lateness_seconds="$max_lateness_seconds" \
  -U "$PGUSER" -d "$PGDB" -P pager=off -c "
WITH parameters AS (
  SELECT make_interval(secs => :'max_lateness_seconds'::integer) AS dispatch_window
)
SELECT
  p.id,
  p.status,
  CASE
    WHEN p.status = 'SCHEDULED'
      AND p.approved_for_publish_at IS NOT NULL
      AND p.approved_for_publish_by IS NOT NULL
      AND p.execution_token IS NULL
      AND p.failure_code IS NULL
      AND p.scheduled_at <= now()
      AND p.scheduled_at >= now() - parameters.dispatch_window
    THEN 'YES'
    ELSE 'NO'
  END AS auto_dispatch_eligible_now,
  to_char(p.scheduled_at AT TIME ZONE 'Europe/Bratislava', 'YYYY-MM-DD HH24:MI')
    AS scheduled_bratislava,
  p.content_version_id,
  c.title,
  p.provider_target_id,
  p.external_id,
  p.failure_code
FROM publications p
JOIN content_items c ON c.id = p.content_item_id
CROSS JOIN parameters
WHERE p.channel::text = 'VK'
ORDER BY p.scheduled_at NULLS FIRST, p.created_at, p.id;
"

printf '\nINVENTORY: PASS\n'
printf '%s\n' \
  '- Read-only PostgreSQL queries: PASS' \
  "- Auto-dispatch window: ${max_lateness_seconds}s (exact runtime setting)" \
  '- VK_PUBLISHING_ENABLED=false' \
  '- Provider calls / DB writes / OpenAI calls / secrets printed: 0'
