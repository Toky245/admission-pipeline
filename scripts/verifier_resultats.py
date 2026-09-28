#!/usr/bin/env python3
"""Compare les décisions de la chaîne avec les résultats attendus.

Lit data/verite_terrain.csv (écrit par le générateur), interroge le service
(GET /bilan) et Mailpit, puis vérifie :
  - que chaque dossier a reçu la bonne décision (complet / incomplet / rejeté) ;
  - que dans chaque paire de doublons, un seul dossier est complet ;
  - qu'aucun doublon n'existe en base ;
  - qu'aucun e-mail n'a été envoyé deux fois.

Aucune dépendance : Python 3.10+.
    python3 scripts/verifier_resultats.py
    python3 scripts/verifier_resultats.py --mailpit http://localhost:8026
"""

from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from collections import Counter
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent


def lire_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--mailpit", default="http://localhost:8026", help="vide pour ne pas vérifier les e-mails")
    p.add_argument("--verite", type=Path, default=RACINE / "data" / "verite_terrain.csv")
    args = p.parse_args()

    attendus = list(csv.DictReader(args.verite.open(encoding="utf-8")))
    bilan = lire_json(f"{args.url}/bilan")
    obtenus = {d["reference"]: d for d in bilan["dossiers"]}

    erreurs, non_traites = [], []
    references_en_paire = {l["groupe"] for l in attendus if l["groupe"]}

    for ligne in attendus:
        ref, attendu = ligne["reference"], ligne["statut_attendu"]
        obtenu = obtenus.get(ref, {}).get("statut")
        if obtenu is None or obtenu == "en_traitement":
            non_traites.append(ref)
            continue
        if ligne["groupe"]:
            origine = obtenus.get(ligne["groupe"], {}).get("statut")
            complets = [obtenu, origine].count("complet")
            if complets != 1:
                erreurs.append(f"{ref} ({ligne['scenario']}) : {complets} dossier(s) complet(s) "
                               f"dans la paire avec {ligne['groupe']} au lieu de 1")
        elif ref in references_en_paire:
            continue   # dossier d'origine : vérifié avec son doublon
        elif obtenu != attendu:
            erreurs.append(f"{ref} ({ligne['scenario']}) : attendu {attendu}, obtenu {obtenu} "
                           f"— {obtenus[ref].get('motif') or ''}")

    print(f"Dossiers attendus : {len(attendus)}   traités : {len(attendus) - len(non_traites)}")
    print("Décisions : " + ", ".join(f"{k} {v}" for k, v in
                                     Counter(obtenus[l['reference']]['statut'] for l in attendus
                                             if l['reference'] in obtenus).items()))
    print(f"Décisions conformes : {len(attendus) - len(non_traites) - len(erreurs)}"
          f" / {len(attendus) - len(non_traites)}")
    for e in erreurs:
        print(f"  ERREUR {e}")
    if non_traites:
        print(f"  {len(non_traites)} dossier(s) pas encore traité(s)")

    print(f"Doublons en base : {len(bilan['doublons'])}")
    for d in bilan["doublons"]:
        print(f"  DOUBLON {d}")

    doublons_mail = 0
    if args.mailpit:
        try:
            messages = lire_json(f"{args.mailpit}/api/v1/messages?limit=100000")["messages"]
            compte = Counter(m.get("MessageID") for m in messages)
            doublons_mail = sum(n - 1 for n in compte.values() if n > 1)
            print(f"E-mails reçus : {len(messages)}   envoyés en double : {doublons_mail}")
        except OSError as e:
            print(f"Mailpit injoignable ({e}) : vérification des e-mails ignorée")

    ok = not erreurs and not bilan["doublons"] and not doublons_mail
    print("RÉSULTAT : " + ("conforme" if ok else "anomalies détectées"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
