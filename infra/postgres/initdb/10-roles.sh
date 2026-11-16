#!/bin/sh
# Runs once, when the data volume is first created. Creates the roles the application
# uses (ADR-014):
# - the migrator owns the database and its objects;
# - the runtime role, used by the API and workers, owns nothing and can't bypass row
#   security;
# - the scheduler role can't log in. It only owns the few SECURITY DEFINER functions
#   that must see every owner (erratum E19), so it bypasses row security. The migrator
#   can create functions as it but doesn't inherit its bypass.
set -eu

psql --no-psqlrc --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname=postgres \
  --set=migrator_password="$DB_MIGRATOR_PASSWORD" \
  --set=runtime_password="$DB_RUNTIME_PASSWORD" <<'SQL'
CREATE ROLE incidentpilot_migrator
  LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'migrator_password';
CREATE ROLE incidentpilot_runtime
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
  PASSWORD :'runtime_password';
CREATE ROLE incidentpilot_scheduler
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS NOINHERIT;
GRANT incidentpilot_scheduler TO incidentpilot_migrator WITH INHERIT FALSE, SET TRUE;
CREATE DATABASE incidentpilot OWNER incidentpilot_migrator;
REVOKE ALL ON DATABASE incidentpilot FROM PUBLIC;
GRANT CONNECT ON DATABASE incidentpilot TO incidentpilot_runtime;
SQL
