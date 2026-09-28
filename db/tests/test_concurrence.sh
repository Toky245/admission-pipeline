#!/bin/sh
# Test de concurrence : 20 exécutions lancées EN MÊME TEMPS demandent
# un matricule pour le même dossier. Un seul matricule doit exister.
# Usage : PGHOST=... PGUSER=... PGDATABASE=... sh db/tests/test_concurrence.sh
set -eu

REF="DEPOT-CONCURRENCE-$$"

psql -qAt -v ON_ERROR_STOP=1 -c "
  INSERT INTO inscription.dossiers (reference, annee_universitaire, parcours_code, statut)
  VALUES ('$REF', '2026-2027', 'ASR', 'complet');" >/dev/null

ID=$(psql -qAt -c "SELECT id FROM inscription.dossiers WHERE reference = '$REF'")
SORTIE=$(mktemp)

i=0
while [ $i -lt 20 ]; do
  psql -qAt -c "SELECT inscription.attribuer_matricule($ID)" >> "$SORTIE" &
  i=$((i + 1))
done
wait

DISTINCTS=$(sort -u "$SORTIE" | wc -l)
TOTAL=$(wc -l < "$SORTIE")
echo "Appels simultanés : $TOTAL  -  matricules distincts obtenus : $DISTINCTS ($(sort -u "$SORTIE"))"

psql -qAt -c "DELETE FROM inscription.dossiers WHERE id = $ID" >/dev/null
rm -f "$SORTIE"

if [ "$DISTINCTS" -ne 1 ] || [ "$TOTAL" -ne 20 ]; then
  echo "ECHEC : l'attribution du matricule n'est pas sûre en concurrence"
  exit 1
fi
echo "Test de concurrence réussi."
