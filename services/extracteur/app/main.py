"""API du service de traitement des dossiers d'inscription.

Appelée par n8n, qui orchestre ; ce service exécute les étapes.

    GET  /sante                                  état du service
    GET  /depots                                 dépôts à traiter
    POST /executions                             ouvre une exécution pour un dossier
    POST /dossiers/{reference}/etapes/{etape}    exécute une étape
    POST /executions/{id}/terminer               clôt l'exécution

Codes de retour : 200 succès (y compris étape sautée ou ignorée),
404 inconnu, 409 appel refusé dans l'état actuel (ne pas relancer),
503 panne technique (à relancer).
"""

import logging

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import executions
from .config import CONFIG
from .db import connexion
from .etapes import ORDRE, Conflit, EchecEtape, Introuvable, executer_etape

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")

app = FastAPI(
    title="Traitement des dossiers d'inscription",
    version="0.2.0",
    description="Étapes idempotentes et reprenables, orchestrées par n8n.",
)


@app.exception_handler(Introuvable)
def _introuvable(_: Request, e: Introuvable) -> JSONResponse:
    return JSONResponse(status_code=404, content={"erreur": str(e), "relancer": False})


@app.exception_handler(Conflit)
def _conflit(_: Request, e: Conflit) -> JSONResponse:
    return JSONResponse(status_code=409, content={"erreur": str(e), "relancer": False})


@app.exception_handler(EchecEtape)
def _echec(_: Request, e: EchecEtape) -> JSONResponse:
    return JSONResponse(status_code=503, content={"erreur": str(e), "relancer": True})


@app.exception_handler(psycopg.OperationalError)
def _base(_: Request, e: psycopg.OperationalError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"erreur": f"Base injoignable : {e}", "relancer": True})


DOSSIERS = ("entree", "archives", "en_attente", "rejets", "accuses")


@app.on_event("startup")
def _creer_dossiers() -> None:
    for nom in DOSSIERS:
        getattr(CONFIG, nom).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
@app.get("/sante")
def sante() -> JSONResponse:
    verifications = {"base": _base_joignable()}
    for nom in DOSSIERS:
        verifications[f"dossier_{nom}"] = getattr(CONFIG, nom).is_dir()
    ok = all(verifications.values())
    return JSONResponse(status_code=200 if ok else 503,
                        content={"statut": "ok" if ok else "degrade", "verifications": verifications})


def _base_joignable() -> bool:
    try:
        with connexion() as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


@app.get("/depots")
def depots(limite: int = Query(50, ge=1, le=1000)) -> dict:
    liste = executions.lister_depots(limite)
    return {"nombre": len(liste), "depots": liste, "etapes": ORDRE}


class OuvertureExecution(BaseModel):
    reference: str
    workflow: str = "traitement_dossier"
    n8n_execution_id: str | None = None
    declencheur: str = Field("automatique", pattern="^(automatique|manuel|reprise|test)$")


@app.post("/executions")
def ouvrir_execution(corps: OuvertureExecution) -> dict:
    return executions.ouvrir(corps.reference, corps.workflow, corps.n8n_execution_id, corps.declencheur)


class AppelEtape(BaseModel):
    execution_id: int


@app.post("/dossiers/{reference}/etapes/{etape}")
def etape(reference: str, etape: str, corps: AppelEtape) -> dict:
    return executer_etape(reference, etape, corps.execution_id)


class Cloture(BaseModel):
    statut: str = Field(pattern="^(reussie|echouee)$")
    erreur: str | None = None


@app.post("/executions/{execution_id}/terminer")
def terminer_execution(execution_id: int, corps: Cloture) -> dict:
    return executions.terminer(execution_id, corps.statut, corps.erreur)


@app.get("/bilan")
def bilan() -> dict:
    """État de tous les dossiers et contrôle des doublons (pour les vérifications)."""
    with connexion() as conn:
        dossiers = conn.execute(
            """SELECT d.reference, d.statut, d.matricule, d.motif, d.parcours_code,
                      d.recu_le, e.fin AS termine_le
                 FROM inscription.dossiers d
                 LEFT JOIN pilotage.etapes_dossier e
                        ON e.dossier_id = d.id AND e.etape = 'archivage'
                       AND e.statut IN ('terminee', 'ignoree')
                ORDER BY d.reference""").fetchall()
        doublons = conn.execute("SELECT * FROM pilotage.v_controle_doublons").fetchall()
    return {"dossiers": dossiers, "doublons": doublons}


@app.get("/metriques")
def metriques() -> dict:
    """Compteurs instantanés, interrogés en continu pendant la campagne de pannes."""
    with connexion() as conn:
        ligne = conn.execute(
            """SELECT
                 (SELECT count(*) FROM inscription.dossiers)                        AS dossiers,
                 (SELECT count(*) FROM pilotage.v_dossiers_termines)                AS termines,
                 (SELECT count(*) FROM pilotage.executions WHERE statut = 'en_cours')    AS exec_en_cours,
                 (SELECT count(*) FROM pilotage.executions WHERE statut = 'reussie')     AS exec_reussies,
                 (SELECT count(*) FROM pilotage.executions WHERE statut = 'echouee')     AS exec_echouees,
                 (SELECT count(*) FROM pilotage.executions WHERE statut = 'interrompue') AS exec_interrompues,
                 (SELECT count(*) FROM pilotage.etapes_dossier WHERE tentatives > 1)     AS etapes_reprises,
                 (SELECT count(*) FROM pilotage.journal WHERE niveau = 'erreur')         AS echecs_etapes,
                 (SELECT count(*) FROM pilotage.v_controle_doublons)                AS doublons,
                 now() AS horodatage""").fetchone()
        par_etape = conn.execute(
            """SELECT etape, reprises, tentatives_max, duree_moyenne_ms
                 FROM pilotage.v_etapes_stats ORDER BY ordre""").fetchall()
    en_attente_depot = sum(1 for d in CONFIG.entree.iterdir()
                           if d.is_dir() and (d / ".pret").exists()) if CONFIG.entree.is_dir() else 0
    return {**ligne, "depots_en_entree": en_attente_depot,
            "duree_verrou_minutes": CONFIG.duree_verrou_minutes, "etapes": par_etape}
