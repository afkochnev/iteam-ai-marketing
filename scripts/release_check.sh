#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../backend"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://iteam:iteam@postgres:5432/iteam_test}"
echo "Running backend tests against the dedicated test database"
ruff check app
ruff format --check app
mypy app --strict --exclude tests --ignore-missing-imports
pytest -q -o asyncio_mode=auto
DATABASE_URL="$TEST_DATABASE_URL" alembic check

cd ../frontend
npm run lint
npm test -- --run
npm run build
