#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../backend"
export APP_ENV="test"
export TEST_DATABASE_URL="${TEST_DATABASE_URL:-postgresql+asyncpg://iteam:iteam@postgres:5432/iteam_test}"
export TEST_REDIS_URL="${TEST_REDIS_URL:-redis://redis:6379/15}"
export DATABASE_URL="$TEST_DATABASE_URL"
export REDIS_URL="$TEST_REDIS_URL"
echo "Running backend tests with dedicated PostgreSQL and isolated Redis configuration"
ruff check .
ruff format --check .
mypy app --exclude "app/tests"
pytest -q -o asyncio_mode=auto
alembic check

cd ../frontend
npm run lint
npm test -- --run
npm run build
