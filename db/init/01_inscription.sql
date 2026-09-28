-- =====================================================================
-- Schéma « inscription » : les données métier
-- ---------------------------------------------------------------------
-- Principe d'idempotence : chaque entité possède une clé naturelle
-- protégée par une contrainte UNIQUE. Relancer une étape sur la même
-- donnée ne peut donc jamais créer de doublon ; les insertions se font
-- avec ON CONFLICT.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA public;

CREATE SCHEMA IF NOT EXISTS inscription;
SET search_path TO inscription;

-- ---------------------------------------------------------------------
-- Référentiels
-- ---------------------------------------------------------------------
CREATE TABLE parcours (
    code        text PRIMARY KEY,
    libelle     text NOT NULL,
    capacite    integer NOT NULL CHECK (capacite > 0)
);

CREATE TABLE types_piece (
    code         text PRIMARY KEY,
    libelle      text NOT NULL,
    obligatoire  boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------
-- Candidats
-- La clé d'identité (nom + prénoms + date de naissance normalisés)
-- empêche de créer deux fois le même candidat s'il redépose un dossier.
-- ---------------------------------------------------------------------
CREATE TABLE candidats (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    nom             text NOT NULL,
    prenoms         text NOT NULL,
    date_naissance  date NOT NULL,
    email           text NOT NULL,
    telephone       text,
    cle_identite    text NOT NULL UNIQUE,
    cree_le         timestamptz NOT NULL DEFAULT now()
);

-- Normalisation : minuscules, sans accents ni espaces superflus.

CREATE FUNCTION cle_identite(p_nom text, p_prenoms text, p_date date)
RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = inscription, public AS $$
    SELECT lower(regexp_replace(trim(public.unaccent(p_nom)), '\s+', ' ', 'g'))
        || '|' || lower(regexp_replace(trim(public.unaccent(p_prenoms)), '\s+', ' ', 'g'))
        || '|' || to_char(p_date, 'YYYY-MM-DD');
$$;

-- ---------------------------------------------------------------------
-- Dossiers
-- « reference » = identifiant du dépôt (nom du dossier déposé).
-- C'est la clé d'idempotence d'entrée : un même dépôt détecté deux fois
-- par le flux correspond toujours à la même ligne.
-- ---------------------------------------------------------------------
CREATE TABLE dossiers (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reference            text NOT NULL UNIQUE,
    candidat_id          bigint REFERENCES candidats(id),
    parcours_code        text REFERENCES parcours(code),
    annee_universitaire  text NOT NULL CHECK (annee_universitaire ~ '^\d{4}-\d{4}$'),
    statut               text NOT NULL DEFAULT 'recu'
                         CHECK (statut IN ('recu', 'en_traitement', 'incomplet',
                                           'complet', 'rejete', 'archive')),
    matricule            text UNIQUE,
    motif                text,
    recu_le              timestamptz NOT NULL DEFAULT now(),
    mis_a_jour_le        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX dossiers_statut_idx ON dossiers (statut);

-- ---------------------------------------------------------------------
-- Pièces du dossier
-- L'empreinte SHA-256 identifie le contenu du fichier : un fichier
-- identique renvoyé deux fois n'est enregistré qu'une fois.
-- Un fichier différent pour le même type (pièce corrigée) crée une
-- nouvelle version ; seule la plus récente est « courante ».
-- ---------------------------------------------------------------------
CREATE TABLE pieces (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dossier_id         bigint NOT NULL REFERENCES dossiers(id) ON DELETE CASCADE,
    type_piece         text NOT NULL REFERENCES types_piece(code),
    nom_fichier        text NOT NULL,
    empreinte_sha256   text NOT NULL CHECK (empreinte_sha256 ~ '^[0-9a-f]{64}$'),
    taille_octets      bigint NOT NULL CHECK (taille_octets >= 0),
    controle           text NOT NULL DEFAULT 'en_attente'
                       CHECK (controle IN ('en_attente', 'valide', 'invalide')),
    donnees_extraites  jsonb,
    erreur             text,
    courante           boolean NOT NULL DEFAULT true,
    recue_le           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dossier_id, type_piece, empreinte_sha256)
);

-- Une seule version courante par type de pièce et par dossier
CREATE UNIQUE INDEX pieces_une_courante_idx
    ON pieces (dossier_id, type_piece) WHERE courante;

-- ---------------------------------------------------------------------
-- Reçus de paiement des droits
-- Un numéro de reçu ne peut servir qu'à un seul dossier : c'est un
-- contrôle anti-fraude, et la contrainte le garantit même en cas de
-- traitements concurrents.
-- ---------------------------------------------------------------------
CREATE TABLE recus_paiement (
    numero       text PRIMARY KEY,
    dossier_id   bigint NOT NULL UNIQUE REFERENCES dossiers(id) ON DELETE CASCADE,
    montant_ar   integer NOT NULL CHECK (montant_ar > 0),
    date_paiement date NOT NULL,
    enregistre_le timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Attribution du matricule — idempotente
-- Si le dossier a déjà un matricule, on le renvoie tel quel.
-- Le verrou FOR UPDATE empêche deux exécutions simultanées d'en
-- attribuer deux différents au même dossier.
-- Format : AAAA-PARCOURS-NNNNN (ex. 2026-GL-00042)
-- ---------------------------------------------------------------------
CREATE SEQUENCE matricule_seq;

CREATE FUNCTION attribuer_matricule(p_dossier_id bigint)
RETURNS text
LANGUAGE plpgsql
SET search_path = inscription, public AS $$
DECLARE
    v_dossier dossiers%ROWTYPE;
    v_matricule text;
BEGIN
    SELECT * INTO v_dossier FROM dossiers WHERE id = p_dossier_id FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Dossier % introuvable', p_dossier_id;
    END IF;

    IF v_dossier.matricule IS NOT NULL THEN
        RETURN v_dossier.matricule;       -- déjà attribué : rien à faire
    END IF;

    IF v_dossier.statut <> 'complet' THEN
        RAISE EXCEPTION 'Dossier % non complet (statut : %)', p_dossier_id, v_dossier.statut;
    END IF;

    v_matricule := format('%s-%s-%s',
                          left(v_dossier.annee_universitaire, 4),
                          v_dossier.parcours_code,
                          lpad(nextval('matricule_seq')::text, 5, '0'));

    UPDATE dossiers
       SET matricule = v_matricule, mis_a_jour_le = now()
     WHERE id = p_dossier_id;

    RETURN v_matricule;
END;
$$;

-- Mise à jour automatique de mis_a_jour_le
CREATE FUNCTION maj_horodatage() RETURNS trigger
LANGUAGE plpgsql
SET search_path = inscription, public AS $$
BEGIN
    NEW.mis_a_jour_le := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER dossiers_maj_horodatage
    BEFORE UPDATE ON dossiers
    FOR EACH ROW EXECUTE FUNCTION maj_horodatage();
