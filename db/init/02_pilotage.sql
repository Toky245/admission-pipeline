-- =====================================================================
-- Schéma « pilotage » : état d'avancement, journal, notifications, alertes
-- ---------------------------------------------------------------------
-- Principe de reprise partielle : l'état de chaque étape est stocké
-- PAR DOSSIER (et non par exécution). Quand un flux est relancé, il
-- demande pour chaque étape « dois-je la faire ? » ; les étapes déjà
-- terminées sont sautées et leur résultat est réutilisé.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS pilotage;
SET search_path TO pilotage;

-- Liste ordonnée des étapes du traitement d'un dossier
CREATE TABLE etapes_ref (
    code    text PRIMARY KEY,
    ordre   smallint NOT NULL UNIQUE,
    libelle text NOT NULL
);

-- ---------------------------------------------------------------------
-- Exécutions : une ligne par lancement d'un flux
-- ---------------------------------------------------------------------
CREATE TABLE executions (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow          text NOT NULL,
    n8n_execution_id  text,
    dossier_id        bigint REFERENCES inscription.dossiers(id) ON DELETE SET NULL,
    declencheur       text NOT NULL DEFAULT 'automatique'
                      CHECK (declencheur IN ('automatique', 'manuel', 'reprise', 'test')),
    statut            text NOT NULL DEFAULT 'en_cours'
                      CHECK (statut IN ('en_cours', 'reussie', 'echouee', 'interrompue')),
    debut             timestamptz NOT NULL DEFAULT now(),
    fin               timestamptz,
    erreur            text
);

CREATE INDEX executions_debut_idx ON executions (debut);
CREATE INDEX executions_workflow_statut_idx ON executions (workflow, statut, debut);

-- ---------------------------------------------------------------------
-- État des étapes par dossier (support de la reprise partielle)
-- ---------------------------------------------------------------------
CREATE TABLE etapes_dossier (
    dossier_id        bigint NOT NULL REFERENCES inscription.dossiers(id) ON DELETE CASCADE,
    etape             text NOT NULL REFERENCES etapes_ref(code),
    statut            text NOT NULL
                      CHECK (statut IN ('en_cours', 'terminee', 'echouee', 'ignoree')),
    tentatives        integer NOT NULL DEFAULT 0,
    resultat          jsonb,
    derniere_erreur   text,
    execution_id      bigint REFERENCES executions(id) ON DELETE SET NULL,
    debut             timestamptz,
    fin               timestamptz,
    PRIMARY KEY (dossier_id, etape)
);

-- ---------------------------------------------------------------------
-- Journal : trace append-only de tout ce qui se passe
-- ---------------------------------------------------------------------
CREATE TABLE journal (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    horodatage    timestamptz NOT NULL DEFAULT now(),
    niveau        text NOT NULL CHECK (niveau IN ('debug', 'info', 'avertissement', 'erreur')),
    execution_id  bigint REFERENCES executions(id) ON DELETE SET NULL,
    dossier_id    bigint REFERENCES inscription.dossiers(id) ON DELETE SET NULL,
    etape         text,
    message       text NOT NULL,
    details       jsonb
);

CREATE INDEX journal_horodatage_idx ON journal (horodatage);
CREATE INDEX journal_dossier_idx ON journal (dossier_id, horodatage);

-- ---------------------------------------------------------------------
-- Notifications (e-mails) — idempotentes
-- La clé est calculée par le flux, ex. 'accuse:<dossier_id>' ou
-- 'recap:2026-S40'. Avant d'envoyer, on réserve la clé ; si elle
-- existe déjà avec le statut 'envoye', on n'envoie pas une 2e fois.
-- ---------------------------------------------------------------------
CREATE TABLE notifications (
    cle           text PRIMARY KEY,
    dossier_id    bigint REFERENCES inscription.dossiers(id) ON DELETE CASCADE,
    type          text NOT NULL CHECK (type IN ('accuse_reception', 'pieces_manquantes',
                                                'rejet', 'recapitulatif', 'alerte')),
    destinataire  text NOT NULL,
    statut        text NOT NULL DEFAULT 'reserve'
                  CHECK (statut IN ('reserve', 'envoye', 'echec')),
    tentatives    integer NOT NULL DEFAULT 0,
    reserve_le    timestamptz NOT NULL DEFAULT now(),
    envoye_le     timestamptz
);

-- ---------------------------------------------------------------------
-- Alertes : échecs répétés détectés
-- ---------------------------------------------------------------------
CREATE TABLE alertes (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow      text NOT NULL,
    nb_echecs     integer NOT NULL,
    fenetre       interval NOT NULL,
    message       text NOT NULL,
    creee_le      timestamptz NOT NULL DEFAULT now(),
    acquittee_le  timestamptz
);

-- =====================================================================
-- Fonctions appelées par n8n
-- =====================================================================

-- Démarre une étape pour un dossier.
-- Renvoie a_faire = false si l'étape est déjà terminée : le flux saute
-- alors l'étape et réutilise « resultat ». Sinon incrémente le compteur
-- de tentatives et marque l'étape en cours.
CREATE FUNCTION debuter_etape(p_dossier_id bigint, p_etape text, p_execution_id bigint)
RETURNS TABLE (a_faire boolean, tentative integer, resultat jsonb)
LANGUAGE plpgsql
SET search_path = pilotage, inscription, public AS $$
DECLARE
    v etapes_dossier%ROWTYPE;
BEGIN
    -- Crée la ligne si elle n'existe pas, puis la verrouille
    INSERT INTO etapes_dossier (dossier_id, etape, statut)
    VALUES (p_dossier_id, p_etape, 'en_cours')
    ON CONFLICT (dossier_id, etape) DO NOTHING;

    SELECT * INTO v FROM etapes_dossier
     WHERE dossier_id = p_dossier_id AND etape = p_etape
       FOR UPDATE;

    IF v.statut IN ('terminee', 'ignoree') THEN
        INSERT INTO journal (niveau, execution_id, dossier_id, etape, message)
        VALUES ('info', p_execution_id, p_dossier_id, p_etape, 'Étape déjà terminée : sautée');
        RETURN QUERY SELECT false, v.tentatives, v.resultat;
        RETURN;
    END IF;

    UPDATE etapes_dossier
       SET statut = 'en_cours',
           tentatives = v.tentatives + 1,
           execution_id = p_execution_id,
           debut = now(),
           fin = NULL
     WHERE dossier_id = p_dossier_id AND etape = p_etape;

    INSERT INTO journal (niveau, execution_id, dossier_id, etape, message, details)
    VALUES ('info', p_execution_id, p_dossier_id, p_etape, 'Étape démarrée',
            jsonb_build_object('tentative', v.tentatives + 1));

    RETURN QUERY SELECT true, v.tentatives + 1, NULL::jsonb;
END;
$$;

CREATE FUNCTION terminer_etape(p_dossier_id bigint, p_etape text,
                               p_execution_id bigint, p_resultat jsonb DEFAULT NULL)
RETURNS void
LANGUAGE plpgsql
SET search_path = pilotage, inscription, public AS $$
BEGIN
    UPDATE etapes_dossier
       SET statut = 'terminee', resultat = p_resultat,
           derniere_erreur = NULL, fin = now()
     WHERE dossier_id = p_dossier_id AND etape = p_etape;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Étape % non démarrée pour le dossier %', p_etape, p_dossier_id;
    END IF;

    INSERT INTO journal (niveau, execution_id, dossier_id, etape, message, details)
    VALUES ('info', p_execution_id, p_dossier_id, p_etape, 'Étape terminée', p_resultat);
END;
$$;

CREATE FUNCTION echouer_etape(p_dossier_id bigint, p_etape text,
                              p_execution_id bigint, p_erreur text)
RETURNS void
LANGUAGE plpgsql
SET search_path = pilotage, inscription, public AS $$
BEGIN
    UPDATE etapes_dossier
       SET statut = 'echouee', derniere_erreur = p_erreur, fin = now()
     WHERE dossier_id = p_dossier_id AND etape = p_etape;

    INSERT INTO journal (niveau, execution_id, dossier_id, etape, message, details)
    VALUES ('erreur', p_execution_id, p_dossier_id, p_etape, 'Étape échouée',
            jsonb_build_object('erreur', p_erreur));
END;
$$;

-- Réserve l'envoi d'une notification.
-- Renvoie true s'il faut envoyer, false si déjà envoyée.
CREATE FUNCTION reserver_notification(p_cle text, p_dossier_id bigint,
                                      p_type text, p_destinataire text)
RETURNS boolean
LANGUAGE plpgsql
SET search_path = pilotage, inscription, public AS $$
DECLARE
    v_statut text;
BEGIN
    INSERT INTO notifications (cle, dossier_id, type, destinataire)
    VALUES (p_cle, p_dossier_id, p_type, p_destinataire)
    ON CONFLICT (cle) DO NOTHING;

    SELECT statut INTO v_statut FROM notifications WHERE cle = p_cle FOR UPDATE;

    IF v_statut = 'envoye' THEN
        RETURN false;
    END IF;

    UPDATE notifications SET tentatives = tentatives + 1, statut = 'reserve'
     WHERE cle = p_cle;
    RETURN true;
END;
$$;

CREATE FUNCTION confirmer_notification(p_cle text)
RETURNS void
LANGUAGE sql
SET search_path = pilotage, inscription, public AS $$
    UPDATE pilotage.notifications
       SET statut = 'envoye', envoye_le = now()
     WHERE cle = p_cle;
$$;

-- =====================================================================
-- Vues pour le tableau de bord
-- =====================================================================

CREATE VIEW v_avancement_dossiers AS
SELECT d.id AS dossier_id,
       d.reference,
       d.statut AS statut_dossier,
       d.matricule,
       count(e.*) FILTER (WHERE e.statut = 'terminee') AS etapes_terminees,
       (SELECT count(*) FROM etapes_ref) AS etapes_totales,
       max(e.tentatives) AS tentatives_max,
       (SELECT r.code FROM etapes_ref r
         WHERE NOT EXISTS (SELECT 1 FROM etapes_dossier x
                            WHERE x.dossier_id = d.id AND x.etape = r.code
                              AND x.statut IN ('terminee', 'ignoree'))
         ORDER BY r.ordre LIMIT 1) AS prochaine_etape,
       d.mis_a_jour_le
  FROM inscription.dossiers d
  LEFT JOIN etapes_dossier e ON e.dossier_id = d.id
 GROUP BY d.id;

CREATE VIEW v_executions_par_heure AS
SELECT date_trunc('hour', debut) AS heure,
       workflow,
       count(*) AS total,
       count(*) FILTER (WHERE statut = 'reussie') AS reussies,
       count(*) FILTER (WHERE statut = 'echouee') AS echouees,
       round(100.0 * count(*) FILTER (WHERE statut = 'reussie') / count(*), 1) AS taux_reussite,
       avg(fin - debut) FILTER (WHERE fin IS NOT NULL) AS duree_moyenne
  FROM executions
 GROUP BY 1, 2;

-- Détection des doublons : doit toujours renvoyer zéro ligne.
-- C'est le test ultime de l'idempotence après la campagne de pannes.
-- (Les e-mails en double se vérifient côté Mailpit : chaque message
--  porte l'en-tête X-Cle-Idempotence, compté par le script de pannes.)
CREATE VIEW v_controle_doublons AS
SELECT 'candidat en double' AS anomalie, cle_identite AS objet, count(*) AS nb
  FROM inscription.candidats GROUP BY cle_identite HAVING count(*) > 1
UNION ALL
SELECT 'matricule en double', matricule, count(*)
  FROM inscription.dossiers WHERE matricule IS NOT NULL GROUP BY matricule HAVING count(*) > 1
UNION ALL
SELECT 'candidat avec plusieurs matricules', c.cle_identite, count(DISTINCT d.matricule)
  FROM inscription.dossiers d JOIN inscription.candidats c ON c.id = d.candidat_id
 WHERE d.matricule IS NOT NULL AND d.statut <> 'rejete'
 GROUP BY c.cle_identite, d.annee_universitaire HAVING count(DISTINCT d.matricule) > 1;
