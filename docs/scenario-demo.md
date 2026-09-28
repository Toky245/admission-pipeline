# Scénario de la vidéo de démonstration (3 minutes)

Objectif : montrer en moins de 3 minutes que la chaîne résiste à une panne
sans perdre ni dupliquer de dossier.

## Préparation (hors caméra)

```bash
make demarrer && make tableau
make vider
```

Ouvrir quatre onglets : n8n (onglet *Executions*), Grafana, Mailpit, un
terminal. Vérifier que le flux n8n est **publié**.

## Déroulé

| Temps | À l'écran | À dire |
|-------|-----------|--------|
| 0:00 – 0:20 | Grafana, tableau vide | « Une université reçoit des centaines de dossiers d'inscription. Je les traite automatiquement, et surtout de façon fiable. » |
| 0:20 – 0:40 | Terminal : `make generer N=20` | « Je dépose 20 dossiers fictifs, dont certains volontairement incomplets ou incohérents. » |
| 0:40 – 1:00 | n8n : une exécution apparaît seule | « n8n passe chaque minute ; chaque dossier traverse 8 étapes. » |
| 1:00 – 1:15 | Mailpit : les e-mails arrivent | « Chaque candidat reçoit son matricule, ou la liste des pièces manquantes. » |
| 1:15 – 1:30 | Terminal : `docker compose stop mailpit` puis `make generer N=10` | « Je coupe le serveur mail, et de nouveaux dossiers arrivent. » |
| 1:30 – 1:55 | n8n : relances puis échecs ; Grafana : « En attente de reprise » passe à l'orange | « Trois essais, puis l'échec est enregistré. Rien n'est perdu : le dossier attend. » |
| 1:55 – 2:10 | Terminal : `docker compose start mailpit` | « Je rétablis le serveur. » |
| 2:10 – 2:35 | n8n : étapes 1 à 6 « sautee », 7 et 8 « terminee » ; Grafana repasse au vert | « La chaîne reprend exactement là où elle s'était arrêtée, sans refaire le reste. » |
| 2:35 – 2:50 | Terminal : `make verifier` → `RÉSULTAT : conforme`, 0 doublon | « Toutes les décisions sont justes, aucun e-mail n'a été envoyé deux fois. » |
| 2:50 – 3:00 | README sur GitHub | « Code, architecture et campagne de pannes chiffrée sont sur GitHub. » |

## Conseils

- Enregistrer en 1920 × 1080, zoom navigateur à 110 %.
- Couper les temps d'attente au montage (la minute entre deux passages n8n).
- Sous-titres conseillés : la vidéo sera souvent regardée sans le son.
- Outils libres : OBS Studio pour l'enregistrement, Kdenlive pour le montage.
