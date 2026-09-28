#!/usr/bin/env python3
"""Génère de faux dossiers d'inscription pour tester la chaîne.

Chaque dossier est un sous-dossier de data/entree/ contenant :
    formulaire.pdf, releve_bac.pdf, attestation.pdf, recu.pdf, photo.png

Une partie des dossiers contient volontairement une erreur (pièce
manquante, fichier corrompu, incohérence, reçu réutilisé...). Le résultat
attendu pour chaque dossier est écrit dans data/verite_terrain.csv : c'est
la référence qui permettra de mesurer si la chaîne a pris la bonne décision.

Le fichier « .pret » est écrit en DERNIER : tant qu'il est absent, le dépôt
est considéré comme en cours de copie et n'est pas traité.

Aucune dépendance à installer : Python 3.10+ suffit.

Exemples :
    python3 scripts/generer_dossiers.py                 # 30 dossiers
    python3 scripts/generer_dossiers.py -n 200 --graine 7
    python3 scripts/generer_dossiers.py -n 20 --sans-erreur
"""

from __future__ import annotations

import argparse
import csv
import random
import struct
import sys
import unicodedata
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "services" / "extracteur"))
from app.pdf_simple import construire_pdf, ecrire_atomique  # noqa: E402

DROITS_AR = 150_000
ANNEE = "2026-2027"
PIED = "Document entièrement fictif généré pour des tests — aucune valeur administrative."

NOMS = [
    "RAKOTOARISOA", "RANDRIANARIVELO", "RAZAFINDRAKOTO", "ANDRIAMAHEFA",
    "RASOLOFONIRINA", "RABEMANANJARA", "RAHARIMALALA", "RANDRIAMAMPIONONA",
    "RAKOTONDRABE", "RAVELOJAONA", "ANDRIANTSOA", "RAZANAMPARANY",
    "RAMANANTSOA", "RASOARIMANANA", "RAJAONARISON", "RANAIVOSON",
]
PRENOMS = [
    "Hery", "Fanja", "Tiana", "Mialy", "Njaka", "Faly", "Rija", "Voahirana",
    "Lova", "Tahina", "Nomena", "Sitraka", "Mahery", "Ony", "Haja",
    "Toavina", "Fitia", "Andry", "Hervé", "Élodie", "Jean Aimé", "Marie Hélène",
]
SERIES = ["A1", "A2", "C", "D", "L", "S", "OSE"]
PARCOURS = ["GL", "ASR", "IG", "OCC"]

# Scénarios d'erreur : (nom, poids, statut attendu)
SCENARIOS = [
    ("piece_manquante",    10, "incomplet"),
    ("photo_corrompue",     5, "incomplet"),
    ("pdf_corrompu",        5, "incomplet"),
    ("montant_incorrect",   4, "incomplet"),
    ("nom_incoherent",      5, "rejete"),
    ("recu_reutilise",      4, "un_seul_complet"),
    ("redepot_candidat",    4, "un_seul_complet"),
    ("variation_ecriture",  8, "complet"),
]


def sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def mention(moyenne: float) -> str:
    if moyenne >= 16:
        return "Très bien"
    if moyenne >= 14:
        return "Bien"
    if moyenne >= 12:
        return "Assez bien"
    return "Passable"


def photo_png(graine: int, largeur: int = 60, hauteur: int = 80) -> bytes:
    """Petite image PNG unie (dégradé), construite avec la bibliothèque standard."""
    rng = random.Random(graine)
    r, g, b = rng.randrange(60, 200), rng.randrange(60, 200), rng.randrange(60, 200)
    lignes = b"".join(
        b"\x00" + bytes((min(255, r + y), g, b)) * largeur for y in range(hauteur)
    )

    def bloc(type_: bytes, donnees: bytes) -> bytes:
        return (struct.pack(">I", len(donnees)) + type_ + donnees
                + struct.pack(">I", zlib.crc32(type_ + donnees) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + bloc(b"IHDR", struct.pack(">IIBBBBB", largeur, hauteur, 8, 2, 0, 0, 0))
            + bloc(b"IDAT", zlib.compress(lignes))
            + bloc(b"IEND", b""))


def cle_identite(nom: str, prenoms: str, naissance: date) -> str:
    """Même normalisation que inscription.cle_identite() en base."""
    norm = lambda t: " ".join(sans_accents(t).lower().split())
    return f"{norm(nom)}|{norm(prenoms)}|{naissance.isoformat()}"


def nouveau_candidat(rng: random.Random, deja_pris: set[str]) -> dict:
    """Candidat inédit : son identité n'a jamais été générée auparavant.

    Sans cette garantie, deux candidats « normaux » pourraient avoir le même
    nom, les mêmes prénoms et la même date de naissance ; le second serait
    alors rejeté à juste titre comme déjà inscrit, et faussera les mesures.
    """
    while True:
        nom = rng.choice(NOMS)
        prenoms = rng.choice(PRENOMS)
        naissance = date(2004, 1, 1) + timedelta(days=rng.randrange(0, 5 * 365))
        cle = cle_identite(nom, prenoms, naissance)
        if cle not in deja_pris:
            deja_pris.add(cle)
            break
    moyenne = round(rng.uniform(10, 18), 2)
    identifiant = sans_accents(prenoms.split()[0]).lower()
    return {
        "nom": nom,
        "prenoms": prenoms,
        "naissance": naissance,
        "email": f"{identifiant}.{nom.lower()}.{rng.randrange(100, 999)}@exemple.test",
        "telephone": f"+261 3{rng.choice('2348')} {rng.randrange(10, 99)} {rng.randrange(100, 999)} {rng.randrange(10, 99)}",
        "parcours": rng.choice(PARCOURS),
        "serie": rng.choice(SERIES),
        "moyenne": moyenne,
        "num_bac": f"BAC-{rng.randrange(100000, 999999)}",
    }


def pieces(c: dict, recu: dict, variante_formulaire: dict | None = None) -> dict[str, bytes]:
    f = {**c, **(variante_formulaire or {})}
    naissance = c["naissance"].strftime("%d/%m/%Y")
    return {
        "formulaire.pdf": construire_pdf("FORMULAIRE D'INSCRIPTION EN PREMIÈRE ANNÉE", [
            f"Nom : {f['nom']}",
            f"Prénoms : {f['prenoms']}",
            f"Date de naissance : {naissance}",
            f"Email : {c['email']}",
            f"Téléphone : {c['telephone']}",
            f"Parcours demandé : {c['parcours']}",
            f"Année universitaire : {ANNEE}",
        ], PIED),
        "releve_bac.pdf": construire_pdf("RELEVÉ DE NOTES DU BACCALAURÉAT", [
            f"Nom : {c['nom']}",
            f"Prénoms : {c['prenoms']}",
            f"Date de naissance : {naissance}",
            f"Numéro d'inscription : {c['num_bac']}",
            f"Série : {c['serie']}",
            f"Moyenne générale : {c['moyenne']:.2f}".replace(".", ","),
        ], PIED),
        "attestation.pdf": construire_pdf("ATTESTATION DE RÉUSSITE AU BACCALAURÉAT", [
            f"Nom : {c['nom']}",
            f"Prénoms : {c['prenoms']}",
            f"Série : {c['serie']}",
            f"Mention : {mention(c['moyenne'])}",
            "Session : 2026",
        ], PIED),
        "recu.pdf": construire_pdf("REÇU DE PAIEMENT DES DROITS D'INSCRIPTION", [
            f"Numéro de reçu : {recu['numero']}",
            f"Payeur : {c['nom']} {c['prenoms']}",
            f"Montant : {recu['montant']} Ar",
            f"Date de paiement : {recu['date'].strftime('%d/%m/%Y')}",
        ], PIED),
        "photo.png": photo_png(zlib.crc32(c["email"].encode())),
    }


def deposer(dossier: Path, fichiers: dict[str, bytes], marqueur: bool = True) -> None:
    dossier.mkdir(parents=True, exist_ok=False)
    for nom, contenu in fichiers.items():
        ecrire_atomique(dossier / nom, contenu)
    # Marqueur écrit en dernier : le dépôt est complet et peut être traité.
    if marqueur:
        ecrire_atomique(dossier / ".pret", b"")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--nombre", type=int, default=30, help="nombre de dossiers (défaut : 30)")
    p.add_argument("--graine", type=int, default=None, help="graine aléatoire, pour un jeu reproductible")
    p.add_argument("--taux-erreurs", type=float, default=0.35, help="part de dossiers avec un scénario (défaut : 0.35)")
    p.add_argument("--sans-erreur", action="store_true", help="aucun scénario d'erreur")
    p.add_argument("--sortie", type=Path, default=RACINE / "data" / "entree")
    p.add_argument("--lot", default=None, help="nom du lot, repris dans les références (défaut : horodatage)")
    p.add_argument("--sans-marqueur", action="store_true",
                   help="ne pas écrire .pret (pour simuler un dépôt encore en cours de copie)")
    args = p.parse_args()

    rng = random.Random(args.graine)
    lot = args.lot or datetime.now().strftime("%Y%m%d-%H%M%S")
    prefixe_recu = f"{zlib.crc32(lot.encode()) % 1_000_000:06d}"
    registre = args.sortie.resolve().parent / "identites_generees.txt"
    deja_pris = set(registre.read_text(encoding="utf-8").split("\n")) if registre.exists() else set()
    args.sortie.mkdir(parents=True, exist_ok=True)
    verite = args.sortie.resolve().parent / "verite_terrain.csv"
    nouveau_fichier = not verite.exists()

    # Dossiers normaux déjà déposés, réutilisables pour créer des doublons
    deja_emis: list[tuple[str, dict, dict]] = []   # (référence, candidat, reçu)
    stats: dict[str, int] = {}

    with verite.open("a", newline="", encoding="utf-8") as fv:
        ecrivain = csv.writer(fv)
        if nouveau_fichier:
            # groupe = référence du dossier d'origine pour les doublons :
            # dans chaque paire (origine, doublon), un seul doit être complet.
            ecrivain.writerow(["reference", "scenario", "statut_attendu", "groupe", "detail"])

        for i in range(1, args.nombre + 1):
            reference = f"DEP-{lot}-{i:04d}"
            candidat = nouveau_candidat(rng, deja_pris)
            recu = {
                # Unique par construction : lot + rang dans le lot
                "numero": f"REC-{prefixe_recu}-{i:05d}",
                "montant": DROITS_AR,
                "date": date(2026, 9, 1) + timedelta(days=rng.randrange(0, 25)),
            }
            scenario, attendu, groupe, detail = "normal", "complet", "", ""
            variante = None

            if not args.sans_erreur and rng.random() < args.taux_erreurs:
                noms, poids, statuts = zip(*SCENARIOS)
                scenario = rng.choices(noms, weights=poids)[0]
                attendu = dict(zip(noms, statuts))[scenario]

                if scenario in ("recu_reutilise", "redepot_candidat") and not deja_emis:
                    scenario, attendu = "normal", "complet"   # rien à réutiliser encore

            fichiers = pieces(candidat, recu)

            if scenario == "piece_manquante":
                retiree = rng.choice(["releve_bac.pdf", "attestation.pdf", "recu.pdf", "photo.png"])
                del fichiers[retiree]
                detail = retiree
            elif scenario == "photo_corrompue":
                fichiers["photo.png"] = fichiers["photo.png"][: len(fichiers["photo.png"]) // 3]
            elif scenario == "pdf_corrompu":
                cible = rng.choice(["releve_bac.pdf", "attestation.pdf"])
                fichiers[cible] = rng.randbytes(800)
                detail = cible
            elif scenario == "montant_incorrect":
                recu["montant"] = rng.choice([50_000, 100_000, 120_000])
                fichiers = pieces(candidat, recu)
                detail = f"{recu['montant']} Ar"
            elif scenario == "nom_incoherent":
                autre = rng.choice([p for p in PRENOMS if p != candidat["prenoms"]])
                variante = {"prenoms": autre}
                fichiers = pieces(candidat, recu, variante)
                detail = f"formulaire : {autre}"
            elif scenario == "variation_ecriture":
                variante = {"nom": candidat["nom"].capitalize(),
                            "prenoms": "  " + sans_accents(candidat["prenoms"]).lower() + " "}
                fichiers = pieces(candidat, recu, variante)
                detail = "casse, accents et espaces différents"
            elif scenario == "recu_reutilise":
                ref_orig, _, recu_orig = rng.choice(deja_emis)
                recu = {**recu, "numero": recu_orig["numero"]}
                fichiers = pieces(candidat, recu)
                groupe, detail = ref_orig, f"reçu {recu_orig['numero']}"
            elif scenario == "redepot_candidat":
                ref_orig, cand_orig, _ = rng.choice(deja_emis)
                candidat = {**cand_orig}
                fichiers = pieces(candidat, recu)
                groupe, detail = ref_orig, "même candidat, nouveau reçu"

            deposer(args.sortie / reference, fichiers, marqueur=not args.sans_marqueur)
            if scenario in ("normal", "variation_ecriture"):
                deja_emis.append((reference, candidat, recu))
            ecrivain.writerow([reference, scenario, attendu, groupe, detail])
            stats[scenario] = stats.get(scenario, 0) + 1

    registre.write_text("\n".join(sorted(deja_pris)), encoding="utf-8")
    print(f"{args.nombre} dossiers générés dans {args.sortie}")
    for nom, nb in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {nom:<20} {nb}")
    print(f"Résultats attendus : {verite}")


if __name__ == "__main__":
    main()
