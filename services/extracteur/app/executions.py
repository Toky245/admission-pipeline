"""Ouverture et clôture des exécutions, liste des dépôts à traiter."""

from __future__ import annotations

from .config import CONFIG
from .db import connexion
from .etapes import Conflit, Introuvable, REFERENCE_VALIDE, verifier_reference


def ouvrir(reference: str, workflow: str, n8n_execution_id: str | None, declencheur: str) -> dict:
    """Crée (ou retrouve) le dossier et ouvre une exécution.

    Refuse (409) si une autre exécution traite déjà ce dossier : deux flux
    n8n lancés en parallèle ne peuvent pas travailler sur le même dossier.
    Une exécution restée « en cours » au-delà du délai est considérée comme
    morte (arrêt brutal) et marquée interrompue, ce qui libère le dossier.
    """
    verifier_reference(reference)
    with connexion() as conn:
        # Verrou transactionnel sur la référence : sérialise les ouvertures concurrentes
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (reference,))

        dossier = conn.execute("SELECT * FROM inscription.dossiers WHERE reference = %s",
                               (reference,)).fetchone()
        if dossier is None:
            if not (CONFIG.entree / reference / ".pret").exists():
                raise Introuvable(f"Aucun dépôt prêt pour {reference}")
            dossier = conn.execute(
                """INSERT INTO inscription.dossiers (reference, annee_universitaire, statut)
                   VALUES (%s, %s, 'en_traitement') RETURNING *""",
                (reference, CONFIG.annee_universitaire)).fetchone()

        interrompues = conn.execute(
            """UPDATE pilotage.executions
                  SET statut = 'interrompue', fin = now(),
                      erreur = 'Aucune activité dans le délai : exécution considérée comme morte'
                WHERE dossier_id = %s AND statut = 'en_cours'
                  AND debut < now() - make_interval(mins => %s)
            RETURNING id""",
            (dossier["id"], CONFIG.duree_verrou_minutes)).fetchall()

        active = conn.execute(
            "SELECT id FROM pilotage.executions WHERE dossier_id = %s AND statut = 'en_cours'",
            (dossier["id"],)).fetchone()
        if active:
            raise Conflit(f"Dossier {reference} déjà en cours de traitement (exécution {active['id']})")

        termine = conn.execute(
            "SELECT 1 FROM pilotage.v_dossiers_termines WHERE dossier_id = %s",
            (dossier["id"],)).fetchone() is not None

        execution_id = conn.execute(
            """INSERT INTO pilotage.executions (workflow, n8n_execution_id, dossier_id, declencheur)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (workflow, n8n_execution_id, dossier["id"], declencheur)).fetchone()["id"]

        for ligne in interrompues:
            conn.execute(
                """INSERT INTO pilotage.journal (niveau, execution_id, dossier_id, message, details)
                   VALUES ('avertissement', %s, %s, 'Exécution précédente interrompue, reprise',
                           jsonb_build_object('execution_interrompue', %s))""",
                (execution_id, dossier["id"], ligne["id"]))

    return {"execution_id": execution_id, "dossier_id": dossier["id"],
            "reference": reference, "deja_termine": termine,
            "reprise": bool(interrompues)}


def terminer(execution_id: int, statut: str, erreur: str | None) -> dict:
    if statut not in ("reussie", "echouee"):
        raise Conflit("statut attendu : reussie ou echouee")
    with connexion() as conn:
        ligne = conn.execute(
            """UPDATE pilotage.executions SET statut = %s, erreur = %s, fin = now()
                WHERE id = %s AND statut = 'en_cours' RETURNING *""",
            (statut, erreur, execution_id)).fetchone()
        if ligne is None:
            # Déjà close (appel rejoué) : on renvoie l'état tel quel
            ligne = conn.execute("SELECT * FROM pilotage.executions WHERE id = %s",
                                 (execution_id,)).fetchone()
            if ligne is None:
                raise Introuvable(f"Exécution {execution_id} inconnue")
    return {"execution_id": ligne["id"], "statut": ligne["statut"],
            "duree_s": round((ligne["fin"] - ligne["debut"]).total_seconds(), 3)}


def lister_depots(limite: int) -> list[dict]:
    """Dépôts à traiter : nouveaux dépôts prêts + dossiers interrompus à reprendre."""
    with connexion() as conn:
        connus = {l["reference"]: l for l in conn.execute(
            """SELECT d.reference,
                      EXISTS (SELECT 1 FROM pilotage.v_dossiers_termines t WHERE t.dossier_id = d.id) AS termine,
                      EXISTS (SELECT 1 FROM pilotage.executions e
                               WHERE e.dossier_id = d.id AND e.statut = 'en_cours'
                                 AND e.debut >= now() - make_interval(mins => %s)) AS occupe
                 FROM inscription.dossiers d""", (CONFIG.duree_verrou_minutes,))}

    resultat = []
    if CONFIG.entree.is_dir():
        for dossier in sorted(CONFIG.entree.iterdir()):
            ref = dossier.name
            if not REFERENCE_VALIDE.match(ref) or not (dossier / ".pret").exists():
                continue
            info = connus.get(ref)
            if info and (info["termine"] or info["occupe"]):
                continue
            resultat.append({"reference": ref, "motif": "reprise" if info else "nouveau"})

    deja = {r["reference"] for r in resultat}
    for ref, info in sorted(connus.items()):
        if ref not in deja and not info["termine"] and not info["occupe"]:
            resultat.append({"reference": ref, "motif": "reprise"})
    return resultat[:limite]
