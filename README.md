# Chaîne d'automatisation des dossiers d'inscription

Automatisation **fiable** du traitement des dossiers de nouveaux étudiants
d'une université (fictive) : réception des pièces, extraction, contrôle,
attribution du matricule, accusé de réception, e-mail au candidat et
archivage.

Automatiser est facile. Ce projet s'intéresse à ce qui se passe quand ça
casse : service indisponible, fichier corrompu, réseau coupé, arrêt brutal
en plein traitement. L'objectif est qu'aucune de ces pannes ne produise de
doublon ni ne passe inaperçue.

> Toutes les données sont fictives et générées par script.

## Ce que le système garantit

- **Idempotence** : relancer un traitement sur le même dossier donne le
  même résultat qu'une seule exécution (pas de second matricule, pas de
  second e-mail, pas de candidat en double).
- **Reprise partielle** : un dossier interrompu à l'étape 6 sur 8 reprend
  à l'étape 6.
- **Traçabilité** : chaque exécution et chaque étape sont journalisées.
- **Alertes** : les échecs répétés déclenchent une notification.

## Pile technique

| Outil                | Rôle                                        |
|----------------------|---------------------------------------------|
| n8n (auto-hébergé)   | Orchestration des flux                      |
| PostgreSQL 16        | Données métier, état d'avancement, journal  |
| Python / FastAPI     | Exécution des étapes (extraction, contrôle…) |
| Mailpit              | Serveur SMTP de test                        |
| Grafana (optionnel)  | Tableau de bord de suivi                    |
| Docker Compose       | Déploiement reproductible                   |

## Démarrage

Prérequis : Docker et Docker Compose.

```bash
git clone https://github.com/Toky245/admission-pipeline.git
cd admission-pipeline
cp .env.example .env        # puis changer les mots de passe
make demarrer               # ou : docker compose up -d --build
make tester                 # vérifie idempotence, reprise et concurrence
```

| Service     | Adresse                        |
|-------------|--------------------------------|
| n8n         | http://localhost:5678          |
| Extracteur  | http://localhost:8000/docs     |
| Mailpit     | http://localhost:8025          |
| Grafana     | http://localhost:3000 (`make demarrer-tout`) |

`make aide` liste toutes les commandes.

## Essayer la chaîne

```bash
make generer N=50     # 50 faux dossiers dans data/entree/, dont ~35 % avec une erreur
make simuler          # traitement sans n8n, par appels directs au service
make verifier         # compare chaque décision au résultat attendu
```

Exemple de sortie de `make verifier` :

```
Dossiers attendus : 100   traités : 100
Décisions : complet 71, rejete 9, incomplet 20
Décisions conformes : 100 / 100
Doublons en base : 0
E-mails reçus : 250   envoyés en double : 0
RÉSULTAT : conforme
```

Les erreurs volontaires du générateur : pièce manquante, photo tronquée,
PDF corrompu, montant du reçu insuffisant, identité incohérente entre les
pièces, reçu réutilisé par un autre candidat, candidat qui redépose un
second dossier, et variations d'écriture (casse, accents, espaces) qui ne
doivent **pas** provoquer de rejet.

## API du service

| Méthode | Route                                    | Rôle                                   |
|---------|------------------------------------------|----------------------------------------|
| GET     | `/depots`                                | Dépôts nouveaux et dossiers à reprendre |
| POST    | `/executions`                            | Ouvre une exécution (verrou par dossier) |
| POST    | `/dossiers/{reference}/etapes/{etape}`   | Exécute une étape                       |
| POST    | `/executions/{id}/terminer`              | Clôt l'exécution                        |
| GET     | `/bilan`                                 | État des dossiers et doublons           |

Codes de retour : `200` succès (étape faite, sautée ou ignorée), `404`
inconnu, `409` refusé dans l'état actuel (ne pas relancer), `503` panne
technique (à relancer). Documentation interactive : `/docs`.

## Structure du dépôt

```
db/
  init/        schéma créé au premier démarrage de PostgreSQL
  tests/       tests d'idempotence, de reprise et de concurrence
services/
  extracteur/  service Python d'extraction des pièces
n8n/workflows/ flux n8n exportés en JSON
grafana/       configuration du tableau de bord
scripts/       générateur, simulateur de flux, vérification des résultats
db/migrations/ évolutions du schéma, rejouables (make migrer)
data/          entree, archives, en_attente, rejets, accuses (non versionnés)
docs/          architecture et décisions techniques
```

## Avancement

- [x] Phase 1 : socle Docker, schéma de base, tests d'idempotence
- [x] Phase 2 : générateur de faux dossiers, 8 étapes idempotentes, simulateur
- [ ] Phase 2 bis : flux n8n équivalent au simulateur
- [ ] Phase 3 : idempotence et reprise branchées dans n8n
- [ ] Phase 4 : relances, flux d'erreur, alertes
- [ ] Phase 5 : tableau de bord Grafana
- [ ] Phase 6 : campagne de pannes et mesures
- [ ] Phase 7 : démonstration vidéo

Détails techniques : [docs/architecture.md](docs/architecture.md)

## Auteur

Toky Aroniaina — [LinkedIn](https://linkedin.com/in/toky-aroniaina) ·
[Portfolio](https://toky-aroniaina.vercel.app/)
