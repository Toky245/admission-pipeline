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


def verifier(url: str, mailpit: str | None, verite: Path,
             references: set[str] | None = None) -> dict:
    """Renvoie le bilan de conformité (limité à `references` si fourni)."""
    attendus = [l for l in csv.DictReader(verite.open(encoding="utf-8"))
                if references is None or l["reference"] in references]
    bilan = lire_json(f"{url}/bilan")
    obtenus = {d["reference"]: d for d in bilan["dossiers"]}
    en_paire = {l["groupe"] for l in attendus if l["groupe"]}

    erreurs, non_traites = [], []
    for ligne in attendus:
        ref, attendu = ligne["reference"], ligne["statut_attendu"]
        obtenu = obtenus.get(ref, {}).get("statut")
        if obtenu is None or obtenu == "en_traitement":
            non_traites.append(ref)
        elif ligne["groupe"]:
            origine = obtenus.get(ligne["groupe"], {}).get("statut")
            complets = [obtenu, origine].count("complet")
            if complets != 1:
                erreurs.append(f"{ref} ({ligne['scenario']}) : {complets} dossier(s) complet(s) "
                               f"dans la paire avec {ligne['groupe']} au lieu de 1")
        elif ref in en_paire:
            continue   # dossier d'origine : vérifié avec son doublon
        elif obtenu != attendu:
            erreurs.append(f"{ref} ({ligne['scenario']}) : attendu {attendu}, obtenu {obtenu} "
                           f"— {obtenus[ref].get('motif') or ''}")

    courriels = None
    if mailpit:
        try:
            messages = lire_json(f"{mailpit}/api/v1/messages?limit=100000")["messages"]
            compte = Counter(m.get("MessageID") for m in messages)
            courriels = {"recus": len(messages),
                         "doublons": sum(n - 1 for n in compte.values() if n > 1)}
        except OSError as e:
            courriels = {"erreur": str(e)}

    traites = len(attendus) - len(non_traites)
    return {
        "attendus": len(attendus), "traites": traites, "non_traites": non_traites,
        "conformes": traites - len(erreurs), "erreurs": erreurs,
        "decisions": Counter(obtenus[l["reference"]]["statut"] for l in attendus
                             if l["reference"] in obtenus),
        "doublons_base": bilan["doublons"], "courriels": courriels,
        "dossiers": {r: obtenus[r] for r in (l["reference"] for l in attendus) if r in obtenus},
        "ok": not erreurs and not bilan["doublons"]
              and not (courriels or {}).get("doublons"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--mailpit", default="http://localhost:8026", help="vide pour ne pas vérifier les e-mails")
    p.add_argument("--verite", type=Path, default=RACINE / "data" / "verite_terrain.csv")
    args = p.parse_args()

    r = verifier(args.url, args.mailpit, args.verite)
    print(f"Dossiers attendus : {r['attendus']}   traités : {r['traites']}")
    print("Décisions : " + ", ".join(f"{k} {v}" for k, v in r["decisions"].items()))
    print(f"Décisions conformes : {r['conformes']} / {r['traites']}")
    for e in r["erreurs"]:
        print(f"  ERREUR {e}")
    if r["non_traites"]:
        print(f"  {len(r['non_traites'])} dossier(s) pas encore traité(s)")
    print(f"Doublons en base : {len(r['doublons_base'])}")
    for d in r["doublons_base"]:
        print(f"  DOUBLON {d}")
    c = r["courriels"]
    if c and "erreur" in c:
        print(f"Mailpit injoignable ({c['erreur']}) : vérification des e-mails ignorée")
    elif c:
        print(f"E-mails reçus : {c['recus']}   envoyés en double : {c['doublons']}")
    print("RÉSULTAT : " + ("conforme" if r["ok"] else "anomalies détectées"))
    raise SystemExit(0 if r["ok"] else 1)


if __name__ == "__main__":
    main()
