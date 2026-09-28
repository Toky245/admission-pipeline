#!/bin/sh
# Premier démarrage : applique aussi toutes les migrations, pour qu'une
# installation neuve soit identique à une base mise à jour avec « make migrer ».
set -eu

for fichier in /migrations/*.sql; do
  [ -e "$fichier" ] || continue
  echo "Migration : $(basename "$fichier")"
  psql -v ON_ERROR_STOP=1 -q --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$fichier"
done
