#!/bin/bash
# Dedicated database for the live demo. Does not write the sysbench application tables.
set -euo pipefail
if ! psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -tAc "SELECT 1 FROM pg_database WHERE datname='demo_live'" | grep -q 1; then
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "CREATE DATABASE demo_live"
fi
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname demo_live <<'SQL'
CREATE SCHEMA IF NOT EXISTS demo_lock;
CREATE TABLE IF NOT EXISTS demo_lock.ticket (
    id integer PRIMARY KEY,
    n integer NOT NULL
);
INSERT INTO demo_lock.ticket (id, n) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
SQL
