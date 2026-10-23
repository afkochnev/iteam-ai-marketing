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
  psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -P pager=off -c "
SELECT
  count(*) FILTER (WHERE p.status = 'PUBLISHING') AS publishing,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.scheduled_at <= now()
      AND p.scheduled_at >= now() - interval '1 hour'
  ) AS due_within_auto_window,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.scheduled_at < now() - interval '1 hour'
  ) AS overdue_outside_auto_window,
  count(*) FILTER (
    WHERE p.status = 'SCHEDULED'
      AND p.scheduled_at > now()
  ) AS future_scheduled,
  count(*) FILTER (
    WHERE p.status IN ('DRAFT', 'WAITING_APPROVAL', 'APPROVED', 'FAILED')
  ) AS other_nonterminal_or_attention
FROM publications p
WHERE p.channel::text = 'VK';
"

printf '\n=== VK PUBLICATION INVENTORY ===\n'
docker compose exec -T postgres \
  psql -X -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -P pager=off -c "
SELECT
  p.id,
  p.status,
  to_char(p.scheduled_at AT TIME ZONE 'Europe/Bratislava', 'YYYY-MM-DD HH24:MI')
    AS scheduled_bratislava,
  p.content_version_id,
  c.title,
  p.provider_target_id,
  p.external_id,
  p.failure_code
FROM publications p
JOIN content_items c ON c.id = p.content_item_id
WHERE p.channel::text = 'VK'
ORDER BY p.scheduled_at NULLS FIRST, p.created_at, p.id;
"

printf '\nINVENTORY: PASS\n'
printf '%s\n' \
  '- Read-only PostgreSQL queries: PASS' \
  '- VK_PUBLISHING_ENABLED=false' \
  '- Provider calls / DB writes / OpenAI calls / secrets printed: 0'
