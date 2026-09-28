#!/bin/sh
# Exécuté une seule fois, au premier démarrage du conteneur PostgreSQL.
# Crée la base interne de n8n et le compte en lecture seule de Grafana.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE DATABASE ${N8N_DB};
  CREATE ROLE ${GRAFANA_DB_USER} LOGIN PASSWORD '${GRAFANA_DB_PASSWORD}';
EOSQL
