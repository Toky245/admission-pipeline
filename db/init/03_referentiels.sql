-- Données de référence (fictives)

INSERT INTO inscription.parcours (code, libelle, capacite) VALUES
    ('GL',  'Génie Logiciel',                           60),
    ('ASR', 'Administration des Systèmes et Réseaux',   60),
    ('IG',  'Informatique Générale',                    80),
    ('OCC', 'Objets Connectés et Cybersécurité',        40);

INSERT INTO inscription.types_piece (code, libelle, obligatoire) VALUES
    ('formulaire',   'Formulaire d''inscription',          true),
    ('releve_bac',   'Relevé de notes du baccalauréat',    true),
    ('attestation',  'Attestation de réussite au bac',     true),
    ('photo',        'Photo d''identité',                  true),
    ('recu',         'Reçu de paiement des droits',        true),
    ('cin',          'Copie de la carte d''identité',      false);

INSERT INTO pilotage.etapes_ref (code, ordre, libelle) VALUES
    ('reception',      1, 'Réception et empreinte des fichiers'),
    ('extraction',     2, 'Extraction des données des pièces'),
    ('controle',       3, 'Contrôle de complétude et de cohérence'),
    ('enregistrement', 4, 'Enregistrement du candidat et du dossier'),
    ('matricule',      5, 'Attribution du matricule'),
    ('accuse',         6, 'Génération de l''accusé de réception'),
    ('notification',   7, 'Envoi de l''e-mail au candidat'),
    ('archivage',      8, 'Archivage du dossier');
