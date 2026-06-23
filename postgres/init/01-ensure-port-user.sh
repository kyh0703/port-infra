#!/usr/bin/env sh
set -eu

app_db="${POSTGRES_DB:-port}"
app_user="${POSTGRES_USER:-port}"
app_password="${POSTGRES_PASSWORD:-port_local_password}"

admin_user=""
for candidate in "$app_user" postgres; do
  if psql --username "$candidate" --dbname postgres -tAc "select 1" >/dev/null 2>&1; then
    admin_user="$candidate"
    break
  fi
done

if [ -z "$admin_user" ]; then
  echo "No local PostgreSQL admin role is available to ensure app database user." >&2
  exit 1
fi

psql \
  --username "$admin_user" \
  --dbname postgres \
  -v ON_ERROR_STOP=1 \
  -v app_db="$app_db" \
  -v app_user="$app_user" \
  -v app_password="$app_password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = :'app_user'
)\gexec

SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'app_user', :'app_password')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'app_db', :'app_user')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_database
  WHERE datname = :'app_db'
)\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_db', :'app_user')\gexec
\connect :app_db
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'app_db', :'app_user')\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_user')\gexec
SQL
