.PHONY: aide demarrer demarrer-tout arreter etat journaux psql doublons tester reinitialiser

aide:            ## Affiche cette aide
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

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

reinitialiser:   ## ATTENTION : supprime toutes les données et relance à vide
	docker compose --profile tableau down -v
	$(MAKE) demarrer
