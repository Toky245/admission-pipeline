-- =====================================================================
-- Tests du schéma : idempotence et reprise partielle
-- Lancement : make tester-schema
-- Tout se déroule dans une transaction annulée à la fin : la base
-- n'est pas modifiée. Chaque ASSERT qui échoue arrête le script.
-- =====================================================================
\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
\echo '1. Un même dépôt détecté deux fois ne crée qu''un seul dossier'
INSERT INTO inscription.dossiers (reference, annee_universitaire)
VALUES ('DEPOT-TEST-001', '2026-2027') ON CONFLICT (reference) DO NOTHING;
INSERT INTO inscription.dossiers (reference, annee_universitaire)
VALUES ('DEPOT-TEST-001', '2026-2027') ON CONFLICT (reference) DO NOTHING;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM inscription.dossiers WHERE reference = 'DEPOT-TEST-001') = 1;
END $$;

-- ---------------------------------------------------------------------
\echo '2. Le même candidat écrit différemment est reconnu'
DO $$ BEGIN
  ASSERT inscription.cle_identite('RAKOTO', 'Jean  Hérvé', '2007-03-14')
       = inscription.cle_identite(' Rakoto', 'jean herve ', '2007-03-14');
  ASSERT inscription.cle_identite('RAKOTO', 'Jean', '2007-03-14')
      <> inscription.cle_identite('RAKOTO', 'Jean', '2007-03-15');
END $$;

INSERT INTO inscription.candidats (nom, prenoms, date_naissance, email, cle_identite)
VALUES ('RAKOTO', 'Jean Hervé', '2007-03-14', 'jean@exemple.test',
        inscription.cle_identite('RAKOTO', 'Jean Hervé', '2007-03-14'))
ON CONFLICT (cle_identite) DO NOTHING;
INSERT INTO inscription.candidats (nom, prenoms, date_naissance, email, cle_identite)
VALUES ('Rakoto', 'jean herve', '2007-03-14', 'jean@exemple.test',
        inscription.cle_identite('Rakoto', 'jean herve', '2007-03-14'))
ON CONFLICT (cle_identite) DO NOTHING;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM inscription.candidats) = 1;
END $$;

UPDATE inscription.dossiers
   SET candidat_id = (SELECT id FROM inscription.candidats), parcours_code = 'GL'
 WHERE reference = 'DEPOT-TEST-001';

-- ---------------------------------------------------------------------
\echo '3. Une pièce identique renvoyée n''est enregistrée qu''une fois'
INSERT INTO inscription.pieces (dossier_id, type_piece, nom_fichier, empreinte_sha256, taille_octets)
SELECT id, 'photo', 'photo.jpg', repeat('a', 64), 1000 FROM inscription.dossiers
ON CONFLICT (dossier_id, type_piece, empreinte_sha256) DO NOTHING;
INSERT INTO inscription.pieces (dossier_id, type_piece, nom_fichier, empreinte_sha256, taille_octets)
SELECT id, 'photo', 'photo-copie.jpg', repeat('a', 64), 1000 FROM inscription.dossiers
ON CONFLICT (dossier_id, type_piece, empreinte_sha256) DO NOTHING;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM inscription.pieces) = 1;
END $$;

-- ---------------------------------------------------------------------
\echo '4. Reprise partielle : une étape terminée est sautée à la relance'
CREATE TEMP TABLE ctx (dossier_id bigint, exec1 bigint);
WITH e AS (INSERT INTO pilotage.executions (workflow) VALUES ('test') RETURNING id)
INSERT INTO ctx SELECT (SELECT id FROM inscription.dossiers), id FROM e;

-- Première exécution : réception réussie, extraction en échec
DO $$
DECLARE c ctx%ROWTYPE; r record;
BEGIN
  SELECT * INTO c FROM ctx;
  SELECT * INTO r FROM pilotage.debuter_etape(c.dossier_id, 'reception', c.exec1);
  ASSERT r.a_faire AND r.tentative = 1;
  PERFORM pilotage.terminer_etape(c.dossier_id, 'reception', c.exec1, '{"fichiers": 5}');

  SELECT * INTO r FROM pilotage.debuter_etape(c.dossier_id, 'extraction', c.exec1);
  ASSERT r.a_faire;
  PERFORM pilotage.echouer_etape(c.dossier_id, 'extraction', c.exec1, 'Service indisponible');
END $$;

-- Relance : la réception est sautée (résultat réutilisé), l'extraction reprend
DO $$
DECLARE c ctx%ROWTYPE; r record;
BEGIN
  SELECT * INTO c FROM ctx;
  SELECT * INTO r FROM pilotage.debuter_etape(c.dossier_id, 'reception', c.exec1);
  ASSERT NOT r.a_faire, 'la réception aurait dû être sautée';
  ASSERT r.resultat = '{"fichiers": 5}'::jsonb, 'le résultat précédent doit être réutilisé';

  SELECT * INTO r FROM pilotage.debuter_etape(c.dossier_id, 'extraction', c.exec1);
  ASSERT r.a_faire AND r.tentative = 2, 'l''extraction doit reprendre en 2e tentative';
END $$;

DO $$ BEGIN
  ASSERT (SELECT prochaine_etape FROM pilotage.v_avancement_dossiers) = 'extraction';
END $$;

-- ---------------------------------------------------------------------
\echo '5. Matricule : refusé si incomplet, attribué une seule fois sinon'
DO $$
DECLARE c ctx%ROWTYPE; m1 text; m2 text; refuse boolean := false;
BEGIN
  SELECT * INTO c FROM ctx;
  BEGIN
    PERFORM inscription.attribuer_matricule(c.dossier_id);
  EXCEPTION WHEN raise_exception THEN refuse := true;
  END;
  ASSERT refuse, 'un dossier non complet ne doit pas recevoir de matricule';

  UPDATE inscription.dossiers SET statut = 'complet' WHERE id = c.dossier_id;
  m1 := inscription.attribuer_matricule(c.dossier_id);
  m2 := inscription.attribuer_matricule(c.dossier_id);
  ASSERT m1 = m2, 'le matricule doit être stable';
  ASSERT m1 ~ '^2026-GL-\d{5}$', 'format inattendu : ' || m1;
END $$;

-- ---------------------------------------------------------------------
\echo '6. Un reçu de paiement ne peut servir qu''à un seul dossier'
INSERT INTO inscription.dossiers (reference, annee_universitaire)
VALUES ('DEPOT-TEST-002', '2026-2027');
INSERT INTO inscription.recus_paiement (numero, dossier_id, montant_ar, date_paiement)
SELECT 'REC-0001', id, 150000, '2026-09-01' FROM inscription.dossiers WHERE reference = 'DEPOT-TEST-001';
DO $$
DECLARE refuse boolean := false;
BEGIN
  BEGIN
    INSERT INTO inscription.recus_paiement (numero, dossier_id, montant_ar, date_paiement)
    SELECT 'REC-0001', id, 150000, '2026-09-01' FROM inscription.dossiers WHERE reference = 'DEPOT-TEST-002';
  EXCEPTION WHEN unique_violation THEN refuse := true;
  END;
  ASSERT refuse, 'le reçu réutilisé aurait dû être refusé';
END $$;

-- ---------------------------------------------------------------------
\echo '7. Une notification déjà envoyée ne repart pas'
DO $$
DECLARE c ctx%ROWTYPE;
BEGIN
  SELECT * INTO c FROM ctx;
  ASSERT pilotage.reserver_notification('accuse:' || c.dossier_id, c.dossier_id, 'accuse_reception', 'jean@exemple.test');
  -- Panne simulée entre la réservation et l'envoi : on peut retenter
  ASSERT pilotage.reserver_notification('accuse:' || c.dossier_id, c.dossier_id, 'accuse_reception', 'jean@exemple.test');
  PERFORM pilotage.confirmer_notification('accuse:' || c.dossier_id);
  -- Après confirmation : plus d'envoi
  ASSERT NOT pilotage.reserver_notification('accuse:' || c.dossier_id, c.dossier_id, 'accuse_reception', 'jean@exemple.test');
END $$;

-- ---------------------------------------------------------------------
\echo '8. La vue de contrôle des doublons est vide'
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM pilotage.v_controle_doublons) = 0;
END $$;

ROLLBACK;
\echo 'Tous les tests du schéma sont passés.'
