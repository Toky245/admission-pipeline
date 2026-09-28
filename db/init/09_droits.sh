#!/bin/sh
# Grafana ne lit que les données : aucun droit d'écriture.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${GRAFANA_DB_USER};
  GRANT USAGE ON SCHEMA inscription, pilotage TO ${GRAFANA_DB_USER};
  GRANT SELECT ON ALL TABLES IN SCHEMA inscription, pilotage TO ${GRAFANA_DB_USER};
  ALTER DEFAULT PRIVILEGES IN SCHEMA inscription, pilotage
    GRANT SELECT ON TABLES TO ${GRAFANA_DB_USER};
EOSQL
