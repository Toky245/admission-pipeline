"""Service d'extraction des pièces de dossiers d'inscription.

Phase 1 : squelette. Seul le point de santé est implémenté ; il vérifie
l'accès à la base et au dossier de données partagé avec n8n.
L'extraction des PDF arrive en phase 2.
"""

import os
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

DATABASE_URL = os.environ["DATABASE_URL"]
DOSSIER_DONNEES = Path(os.environ.get("DOSSIER_DONNEES", "/data"))

app = FastAPI(
    title="Extracteur de dossiers d'inscription",
    version="0.1.0",
)


@app.get("/sante")
def sante() -> JSONResponse:
    """Vérifie que le service peut joindre la base et lire les données."""
    verifications = {
        "base": _base_joignable(),
        "dossier_entree": (DOSSIER_DONNEES / "entree").is_dir(),
    }
    ok = all(verifications.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"statut": "ok" if ok else "degrade", "verifications": verifications},
    )


def _base_joignable() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False
