#!/usr/bin/env sh
set -eu

admin_user="${POSTGRES_USER:-port}"
keycloak_db="${KEYCLOAK_DATABASE_NAME:-keycloak}"
keycloak_user="${KEYCLOAK_DATABASE_USER:-keycloak}"
keycloak_password="${KEYCLOAK_DATABASE_PASSWORD:-keycloak_local_password}"

psql \
  --username "$admin_user" \
  --dbname postgres \
  -v ON_ERROR_STOP=1 \
  -v keycloak_db="$keycloak_db" \
  -v keycloak_user="$keycloak_user" \
  -v keycloak_password="$keycloak_password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'keycloak_user', :'keycloak_password')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_roles
  WHERE rolname = :'keycloak_user'
)\gexec

SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'keycloak_user', :'keycloak_password')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'keycloak_db', :'keycloak_user')
WHERE NOT EXISTS (
  SELECT 1
  FROM pg_database
  WHERE datname = :'keycloak_db'
)\gexec

SELECT format('ALTER DATABASE %I OWNER TO %I', :'keycloak_db', :'keycloak_user')\gexec
\connect :keycloak_db
SELECT format('GRANT ALL PRIVILEGES ON DATABASE %I TO %I', :'keycloak_db', :'keycloak_user')\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'keycloak_user')\gexec
SQL
