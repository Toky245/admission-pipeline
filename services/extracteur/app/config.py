"""Configuration lue depuis les variables d'environnement."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    database_url: str
    donnees: Path
    annee_universitaire: str
    droits_ar: int
    smtp_hote: str
    smtp_port: int
    expediteur: str
    duree_verrou_minutes: int

    @property
    def entree(self) -> Path:
        return self.donnees / "entree"

    @property
    def archives(self) -> Path:
        return self.donnees / "archives"

    @property
    def en_attente(self) -> Path:
        return self.donnees / "en_attente"

    @property
    def rejets(self) -> Path:
        return self.donnees / "rejets"

    @property
    def accuses(self) -> Path:
        return self.donnees / "accuses"


def charger() -> Config:
    return Config(
        database_url=os.environ["DATABASE_URL"],
        donnees=Path(os.environ.get("DOSSIER_DONNEES", "/data")),
        annee_universitaire=os.environ.get("ANNEE_UNIVERSITAIRE", "2026-2027"),
        droits_ar=int(os.environ.get("DROITS_INSCRIPTION_AR", "150000")),
        smtp_hote=os.environ.get("SMTP_HOTE", "mailpit"),
        smtp_port=int(os.environ.get("SMTP_PORT", "1025")),
        expediteur=os.environ.get("EXPEDITEUR", "scolarite@universite-fictive.test"),
        duree_verrou_minutes=int(os.environ.get("DUREE_VERROU_MINUTES", "15")),
    )


CONFIG = charger()
