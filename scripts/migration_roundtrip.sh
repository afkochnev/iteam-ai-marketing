#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must explicitly target a *_test database}"
database_name="${DATABASE_URL##*/}"
database_name="${database_name%%\?*}"
case "$database_name" in
  *_test) ;;
  *)
    echo "Refusing migration round-trip: DATABASE_URL must target a database ending in _test" >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")/../backend"
alembic downgrade base
alembic upgrade head
