# Ports lus dans .env s'il existe
-include .env
EXTRACTEUR_PORT ?= 8000
MAILPIT_UI_PORT ?= 8025
GRAFANA_PORT ?= 3000
N ?= 30

.PHONY: aide demarrer demarrer-tout arreter etat journaux psql doublons tester migrer \
        generer simuler verifier tableau reinitialiser

aide:            ## Affiche cette aide
	@grep -hE '^[a-z-]+:.*##' Makefile | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

.env:
	cp .env.example .env
	@echo "Fichier .env créé : pense à changer les mots de passe."

demarrer: .env   ## Lance la pile (sans Grafana)
	docker compose up -d --build

demarrer-tout: .env  ## Lance la pile avec le tableau de bord Grafana
	docker compose --profile tableau up -d --build

arreter:         ## Arrête les conteneurs (les données sont conservées)
	docker compose --profile tableau down

etat:            ## État des conteneurs
	docker compose --profile tableau ps

journaux:        ## Suit les journaux de tous les services
	docker compose logs -f --tail=100

psql:            ## Ouvre une console SQL sur la base métier
	docker compose exec postgres sh -c 'psql -U $$POSTGRES_USER -d $$POSTGRES_DB'

doublons:        ## Vérifie l'absence de doublons (doit afficher 0 ligne)
	docker compose exec postgres sh -c 'psql -U $$POSTGRES_USER -d $$POSTGRES_DB -c "SELECT * FROM pilotage.v_controle_doublons"'

tester:          ## Lance les tests du schéma (idempotence, reprise, concurrence)
	docker compose exec -T postgres sh -c 'psql -q -U $$POSTGRES_USER -d $$POSTGRES_DB' < db/tests/test_schema.sql
	docker compose exec -T postgres sh -c 'PGUSER=$$POSTGRES_USER PGDATABASE=$$POSTGRES_DB sh -s' < db/tests/test_concurrence.sh

migrer:          ## Applique les migrations SQL (rejouables sans risque)
	@for f in db/migrations/*.sql; do \
	  echo "Migration : $$f"; \
	  docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U $$POSTGRES_USER -d $$POSTGRES_DB' < $$f || exit 1; \
	done

generer:         ## Génère N faux dossiers (ex. make generer N=100)
	python3 scripts/generer_dossiers.py -n $(N)

simuler:         ## Traite les dépôts SANS n8n (référence pour le flux n8n)
	python3 scripts/simuler_flux.py --url http://localhost:$(EXTRACTEUR_PORT)

verifier:        ## Compare les décisions avec les résultats attendus
	python3 scripts/verifier_resultats.py --url http://localhost:$(EXTRACTEUR_PORT) \
	  --mailpit http://localhost:$(MAILPIT_UI_PORT)

tableau:         ## Lance Grafana et affiche l'adresse du tableau de bord
	docker compose --profile tableau up -d grafana
	@echo "Tableau de bord : http://localhost:$(GRAFANA_PORT)  (identifiants dans .env)"

reinitialiser:   ## ATTENTION : efface base, dossiers traités et e-mails, puis relance
	docker compose --profile tableau down -v
	find data -mindepth 2 ! -name .gitkeep -delete 2>/dev/null; rm -f data/verite_terrain.csv
	$(MAKE) demarrer
