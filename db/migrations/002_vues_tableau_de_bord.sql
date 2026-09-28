-- Migration 002 : vues pour le tableau de bord Grafana
-- Idempotente (CREATE OR REPLACE). Lecture seule : aucune donnée modifiée.

-- Dossiers avec leur situation actuelle, en une ligne chacun
CREATE OR REPLACE VIEW pilotage.v_dossiers AS
SELECT d.id,
       d.reference,
       d.statut,
       d.parcours_code,
       d.matricule,
       d.recu_le,
       d.mis_a_jour_le,
       EXISTS (SELECT 1 FROM pilotage.v_dossiers_termines t WHERE t.dossier_id = d.id) AS termine,
       (SELECT r.code FROM pilotage.etapes_ref r
         WHERE NOT EXISTS (SELECT 1 FROM pilotage.etapes_dossier e
                            WHERE e.dossier_id = d.id AND e.etape = r.code
                              AND e.statut IN ('terminee', 'ignoree'))
         ORDER BY r.ordre LIMIT 1) AS etape_en_attente
  FROM inscription.dossiers d;

-- Pièces manquantes ou non conformes relevées au contrôle
CREATE OR REPLACE VIEW pilotage.v_pieces_manquantes AS
SELECT e.dossier_id,
       d.reference,
       e.fin AS horodatage,
       m->>'piece' AS piece,
       m->>'raison' AS raison,
       CASE
           WHEN m->>'raison' = 'absente' THEN 'absente'
           WHEN m->>'raison' LIKE 'illisible%' THEN 'illisible'
           ELSE 'non conforme'
       END AS categorie
  FROM pilotage.etapes_dossier e
  JOIN inscription.dossiers d ON d.id = e.dossier_id
 CROSS JOIN LATERAL jsonb_array_elements(e.resultat->'manquantes') AS m
 WHERE e.etape = 'controle' AND e.statut = 'terminee';

-- Motifs de rejet regroupés par famille
CREATE OR REPLACE VIEW pilotage.v_motifs_rejet AS
SELECT d.id AS dossier_id,
       d.reference,
       d.mis_a_jour_le AS horodatage,
       d.motif,
       CASE
           WHEN d.motif ILIKE '%reçu%déjà utilisé%'      THEN 'Reçu déjà utilisé'
           WHEN d.motif ILIKE '%candidat déjà inscrit%'  THEN 'Candidat déjà inscrit'
           WHEN d.motif ILIKE '%identité différente%'    THEN 'Identité incohérente'
           WHEN d.motif ILIKE '%parcours inconnu%'       THEN 'Parcours inconnu'
           ELSE 'Autre'
       END AS famille
  FROM inscription.dossiers d
 WHERE d.statut = 'rejete';

-- Comportement de chaque étape : volumes, durées, relances
CREATE OR REPLACE VIEW pilotage.v_etapes_stats AS
SELECT r.ordre,
       r.code AS etape,
       r.libelle,
       count(e.*) FILTER (WHERE e.statut = 'terminee') AS terminees,
       count(e.*) FILTER (WHERE e.statut = 'ignoree')  AS ignorees,
       count(e.*) FILTER (WHERE e.statut = 'echouee')  AS en_echec,
       count(e.*) FILTER (WHERE e.statut = 'en_cours') AS en_cours,
       round(avg(extract(epoch FROM e.fin - e.debut) * 1000)
             FILTER (WHERE e.statut = 'terminee')::numeric, 1) AS duree_moyenne_ms,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM e.fin - e.debut) * 1000)
             FILTER (WHERE e.statut = 'terminee'))::numeric, 1) AS duree_p95_ms,
       count(e.*) FILTER (WHERE e.tentatives > 1) AS reprises,
       coalesce(max(e.tentatives), 0) AS tentatives_max
  FROM pilotage.etapes_ref r
  LEFT JOIN pilotage.etapes_dossier e ON e.etape = r.code
 GROUP BY r.ordre, r.code, r.libelle;

-- Échecs d'étapes (historique complet, depuis le journal)
CREATE OR REPLACE VIEW pilotage.v_echecs AS
SELECT j.horodatage,
       j.etape,
       d.reference,
       j.execution_id,
       j.details->>'erreur' AS erreur
  FROM pilotage.journal j
  LEFT JOIN inscription.dossiers d ON d.id = j.dossier_id
 WHERE j.niveau = 'erreur';
