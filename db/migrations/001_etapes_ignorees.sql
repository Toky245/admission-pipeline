-- Migration 001 : étapes ignorées et suivi des exécutions
-- Idempotente : peut être rejouée sans effet de bord (CREATE OR REPLACE).

-- Marque une étape comme non applicable pour ce dossier
-- (ex. pas de matricule pour un dossier incomplet).
-- Une étape ignorée est traitée comme terminée lors d'une reprise.
CREATE OR REPLACE FUNCTION pilotage.ignorer_etape(p_dossier_id bigint, p_etape text,
                                                  p_execution_id bigint, p_raison text)
RETURNS void
LANGUAGE plpgsql
SET search_path = pilotage, inscription, public AS $$
BEGIN
    UPDATE etapes_dossier
       SET statut = 'ignoree',
           resultat = jsonb_build_object('ignoree', true, 'raison', p_raison),
           derniere_erreur = NULL, fin = now()
     WHERE dossier_id = p_dossier_id AND etape = p_etape;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Étape % non démarrée pour le dossier %', p_etape, p_dossier_id;
    END IF;

    INSERT INTO journal (niveau, execution_id, dossier_id, etape, message, details)
    VALUES ('info', p_execution_id, p_dossier_id, p_etape, 'Étape ignorée',
            jsonb_build_object('raison', p_raison));
END;
$$;

-- Un dossier est « terminé » quand sa dernière étape est terminée ou ignorée
CREATE OR REPLACE VIEW pilotage.v_dossiers_termines AS
SELECT dossier_id
  FROM pilotage.etapes_dossier
 WHERE etape = 'archivage' AND statut IN ('terminee', 'ignoree');
