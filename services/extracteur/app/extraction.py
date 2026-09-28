"""Lecture et vérification des pièces d'un dossier."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

# pypdf journalise chaque anomalie des fichiers abîmés : trop bavard ici
logging.getLogger("pypdf").setLevel(logging.CRITICAL)

# Nom de fichier attendu -> type de pièce (référentiel inscription.types_piece)
FICHIERS = {
    "formulaire.pdf": "formulaire",
    "releve_bac.pdf": "releve_bac",
    "attestation.pdf": "attestation",
    "recu.pdf": "recu",
    "photo.png": "photo",
    "photo.jpg": "photo",
    "cin.pdf": "cin",
}

# Pour chaque type : mot-clé attendu dans le titre, champs obligatoires
REGLES = {
    "formulaire": ("FORMULAIRE", ["nom", "prenoms", "date_de_naissance", "email", "parcours_demande"]),
    "releve_bac": ("RELEVE", ["nom", "prenoms", "date_de_naissance", "moyenne_generale"]),
    "attestation": ("ATTESTATION", ["nom", "prenoms"]),
    "recu": ("RECU", ["numero_de_recu", "montant", "date_de_paiement"]),
    "cin": ("", []),
}

LIGNE_CHAMP = re.compile(r"^\s*([^:]{2,60}?)\s*:\s*(.+?)\s*$")


class PieceIllisible(Exception):
    """La pièce est présente mais inexploitable (corrompue, incomplète...)."""


@dataclass
class Fichier:
    type_piece: str
    chemin: Path
    empreinte: str
    taille: int


@dataclass
class Inventaire:
    fichiers: dict[str, Fichier] = field(default_factory=dict)
    inconnus: list[str] = field(default_factory=list)


def sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def cle_champ(libelle: str) -> str:
    """« Numéro de reçu » -> « numero_de_recu »."""
    return re.sub(r"[^a-z0-9]+", "_", sans_accents(libelle).lower()).strip("_")


def empreinte_sha256(chemin: Path) -> str:
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(65536), b""):
            h.update(bloc)
    return h.hexdigest()


def inventorier(dossier: Path) -> Inventaire:
    """Liste les pièces reconnues d'un dépôt, avec leur empreinte."""
    inv = Inventaire()
    for chemin in sorted(dossier.iterdir()):
        if chemin.name.startswith(".") or not chemin.is_file():
            continue
        type_piece = FICHIERS.get(chemin.name.lower())
        if type_piece is None:
            inv.inconnus.append(chemin.name)
            continue
        inv.fichiers[type_piece] = Fichier(type_piece, chemin, empreinte_sha256(chemin), chemin.stat().st_size)
    return inv


def lire_pdf(chemin: Path, type_piece: str) -> dict:
    """Extrait les champs « Clé : valeur » d'un PDF et vérifie les champs obligatoires."""
    contenu = chemin.read_bytes()   # une erreur disque ici est une vraie panne : elle remonte
    try:
        # Le fichier est lu : toute erreur d'analyse vient de son contenu,
        # c'est donc une pièce illisible, pas une panne à relancer.
        lecteur = PdfReader(io.BytesIO(contenu), strict=False)
        if not lecteur.pages:
            raise PieceIllisible("PDF sans page")
        texte = "\n".join((page.extract_text() or "") for page in lecteur.pages)
    except PieceIllisible:
        raise
    except Exception as e:  # noqa: BLE001 — voir commentaire ci-dessus
        raise PieceIllisible(f"PDF illisible : {type(e).__name__}") from e

    lignes = [l for l in texte.splitlines() if l.strip()]
    if not lignes:
        raise PieceIllisible("PDF sans texte exploitable")

    mot_cle, obligatoires = REGLES.get(type_piece, ("", []))
    titre = sans_accents(lignes[0]).upper()
    if mot_cle and mot_cle not in titre:
        raise PieceIllisible(f"document inattendu (titre : « {lignes[0][:60]} »)")

    champs: dict[str, str] = {"titre": lignes[0].strip()}
    for ligne in lignes[1:]:
        m = LIGNE_CHAMP.match(ligne)
        if m:
            champs[cle_champ(m.group(1))] = m.group(2)

    manquants = [c for c in obligatoires if not champs.get(c)]
    if manquants:
        raise PieceIllisible(f"champs manquants : {', '.join(manquants)}")
    return champs


def verifier_image(chemin: Path) -> dict:
    """Vérifie l'intégrité d'une photo PNG (blocs et sommes de contrôle) ou JPEG."""
    donnees = chemin.read_bytes()
    if donnees.startswith(b"\xff\xd8"):
        if not donnees.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise PieceIllisible("JPEG tronqué")
        return {"format": "jpeg", "taille_octets": len(donnees)}

    if not donnees.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PieceIllisible("format d'image non reconnu")

    position, largeur, hauteur, fin_trouvee = 8, 0, 0, False
    while position + 8 <= len(donnees):
        longueur, type_bloc = struct.unpack(">I4s", donnees[position:position + 8])
        fin_bloc = position + 12 + longueur
        if fin_bloc > len(donnees):
            raise PieceIllisible("PNG tronqué")
        contenu = donnees[position + 8:position + 8 + longueur]
        crc_lu = struct.unpack(">I", donnees[position + 8 + longueur:fin_bloc])[0]
        if zlib.crc32(type_bloc + contenu) & 0xFFFFFFFF != crc_lu:
            raise PieceIllisible(f"PNG corrompu (bloc {type_bloc.decode(errors='replace')})")
        if type_bloc == b"IHDR":
            largeur, hauteur = struct.unpack(">II", contenu[:8])
        if type_bloc == b"IEND":
            fin_trouvee = True
            break
        position = fin_bloc

    if not fin_trouvee:
        raise PieceIllisible("PNG tronqué")
    return {"format": "png", "largeur": largeur, "hauteur": hauteur}


def extraire(fichier: Fichier) -> dict:
    if fichier.type_piece == "photo":
        return verifier_image(fichier.chemin)
    return lire_pdf(fichier.chemin, fichier.type_piece)
