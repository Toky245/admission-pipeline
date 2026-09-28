#!/usr/bin/env python3
"""Rejoue le flux de traitement SANS n8n, en appelant le service comme n8n le fait.

Sert de référence (ce que le flux n8n doit reproduire) et d'outil de test.
Pour chaque dépôt : ouvrir une exécution, appeler les 8 étapes dans l'ordre
avec relances sur les erreurs 503, puis clore l'exécution.

Aucune dépendance : Python 3.10+.

Exemples :
    python3 scripts/simuler_flux.py
    python3 scripts/simuler_flux.py --parallele 4        # 4 dossiers à la fois
    python3 scripts/simuler_flux.py --tentatives 5 --delai 2
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import Counter


def appeler(url: str, methode: str = "GET", corps: dict | None = None, delai: float = 30) -> tuple[int, dict]:
    donnees = json.dumps(corps).encode() if corps is not None else None
    requete = urllib.request.Request(url, data=donnees, method=methode,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"erreur": e.reason}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, {"erreur": f"service injoignable : {e}"}


def traiter(base: str, reference: str, etapes: list[str], tentatives: int, attente: float) -> dict:
    code, ouv = appeler(f"{base}/executions", "POST",
                        {"reference": reference, "workflow": "simulateur", "declencheur": "test"})
    if code != 200:
        return {"reference": reference, "resultat": f"non ouvert ({code}) : {ouv.get('erreur')}"}

    execution_id, bilan = ouv["execution_id"], {}
    for etape in etapes:
        for essai in range(1, tentatives + 1):
            code, rep = appeler(f"{base}/dossiers/{reference}/etapes/{etape}", "POST",
                                {"execution_id": execution_id})
            if code == 200:
                bilan[etape] = rep["statut"]
                break
            if code in (404, 409) or essai == tentatives:
                appeler(f"{base}/executions/{execution_id}/terminer", "POST",
                        {"statut": "echouee", "erreur": f"{etape} : {rep.get('erreur')}"})
                return {"reference": reference, "resultat": f"échec à l'étape {etape} : {rep.get('erreur')}",
                        "etapes": bilan}
            time.sleep(attente * essai)   # attente croissante entre deux essais

    appeler(f"{base}/executions/{execution_id}/terminer", "POST", {"statut": "reussie"})
    return {"reference": reference, "resultat": "ok", "etapes": bilan}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--limite", type=int, default=1000)
    p.add_argument("--parallele", type=int, default=1, help="dossiers traités simultanément")
    p.add_argument("--tentatives", type=int, default=3)
    p.add_argument("--delai", type=float, default=1.0, help="attente de base entre deux essais (s)")
    args = p.parse_args()

    code, rep = appeler(f"{args.url}/depots?limite={args.limite}")
    if code != 200:
        raise SystemExit(f"Impossible de lister les dépôts ({code}) : {rep.get('erreur')}")
    references = [d["reference"] for d in rep["depots"]]
    print(f"{len(references)} dépôt(s) à traiter")
    if not references:
        return

    debut = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.parallele) as pool:
        resultats = list(pool.map(
            lambda r: traiter(args.url, r, rep["etapes"], args.tentatives, args.delai), references))
    duree = time.monotonic() - debut

    echecs = [r for r in resultats if r["resultat"] != "ok"]
    for r in echecs:
        print(f"  ECHEC {r['reference']} : {r['resultat']}")
    statuts = Counter(s for r in resultats for s in r.get("etapes", {}).values())
    print(f"Terminé en {duree:.1f} s : {len(resultats) - len(echecs)} réussis, {len(echecs)} en échec")
    print("Étapes : " + ", ".join(f"{k} {v}" for k, v in sorted(statuts.items())))


if __name__ == "__main__":
    main()
