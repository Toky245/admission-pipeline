-- Vide les données du projet (dossiers, exécutions, journal) sans toucher
-- au schéma, aux référentiels, ni à la base interne de n8n.
BEGIN;
TRUNCATE inscription.recus_paiement, inscription.pieces, inscription.dossiers,
         inscription.candidats, pilotage.etapes_dossier, pilotage.executions,
         pilotage.journal, pilotage.notifications, pilotage.alertes
         RESTART IDENTITY CASCADE;
ALTER SEQUENCE inscription.matricule_seq RESTART;
COMMIT;
\echo 'Données du projet vidées (n8n et référentiels conservés).'
