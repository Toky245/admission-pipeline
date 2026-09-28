#!/usr/bin/env python3
"""Campagne de pannes : dépose des centaines de dossiers en provoquant des pannes,
puis mesure si la chaîne a tout traité, sans doublon, et en combien de temps.

Déroulé : six vagues de dossiers. Chaque panne démarre AVANT sa vague, pour
que les dossiers arrivent sur un système en panne :

  vague 1  sans panne (référence)
  vague 2  pendant une PANNE SMTP      (Mailpit arrêté 90 s)
  vague 3  pendant une PANNE SERVICE   (service de traitement arrêté 60 s)
  vague 4  pendant une PANNE RÉSEAU    (service déconnecté du réseau Docker 60 s)
  vague 5  déposée « en cours de copie » : marqueur .pret posé 40 s plus tard
           (aucun de ces dossiers ne doit être traité avant)
  vague 6  puis, dès qu'un traitement est en cours :
           ARRÊT BRUTAL service (kill -9), base PostgreSQL (kill -9), n8n (kill -9)
  enfin    attente que tous les dossiers soient terminés (reprises comprises)

Les fichiers corrompus viennent du générateur (PDF abîmés, photos tronquées...).

Pendant toute la campagne, /metriques est relevé toutes les 5 s. À la fin, un
rapport Markdown et le suivi CSV sont écrits dans docs/resultats/.

Prérequis :
  - la pile tourne (make demarrer) et le flux n8n est publié ;
  - base vide conseillée (make vider) ;
  - DUREE_VERROU_MINUTES=2 dans .env, sinon les reprises après un arrêt
    brutal de n8n attendent 15 minutes.

Exemples :
  python3 scripts/campagne_pannes.py                    # 300 dossiers, avec n8n
  python3 scripts/campagne_pannes.py -n 100 --sans-pannes   # mesure de référence
"""

from __future__ import annotations

import argparse
import csv
import json
import shlex
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "scripts"))
from verifier_resultats import verifier  # noqa: E402


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------
def lire(url: str, delai: float = 5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=delai) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def horodate(dt: datetime | str) -> datetime:
    return dt if isinstance(dt, datetime) else datetime.fromisoformat(dt)


class Campagne:
    def __init__(self, args: argparse.Namespace):
        self.a = args
        self.nom = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.t0 = time.monotonic()
        self.debut = datetime.now().astimezone()
        self.suivi: list[dict] = []
        self.evenements: list[dict] = []
        self.depots: dict[str, datetime] = {}      # référence -> heure où elle est devenue traitable
        self.arret = threading.Event()
        self.marqueurs_poses: datetime | None = None
        self.verite = args.donnees / "verite_terrain.csv"

    # --- temps -------------------------------------------------------------
    def t(self) -> float:
        return time.monotonic() - self.t0

    def attendre_jusqua(self, seconde: float) -> None:
        cible = seconde * self.a.echelle
        while self.t() < cible and not self.arret.is_set():
            time.sleep(min(1.0, cible - self.t()))

    def journal(self, texte: str) -> None:
        print(f"[{self.t():7.1f} s] {texte}", flush=True)

    # --- commandes docker ---------------------------------------------------
    def commande(self, base: str, *arguments: str) -> bool:
        cmd = shlex.split(base) + list(arguments)
        r = subprocess.run(cmd, cwd=RACINE, capture_output=True, text=True)
        if r.returncode != 0:
            self.journal(f"  commande en échec : {' '.join(cmd)} — {r.stderr.strip()[:150]}")
        return r.returncode == 0

    def compose(self, *arguments: str) -> bool:
        return self.commande(self.a.compose, *arguments)

    def docker(self, *arguments: str) -> bool:
        return self.commande(self.a.docker, *arguments)

    # --- suivi continu -------------------------------------------------------
    def surveiller(self) -> None:
        while not self.arret.is_set():
            m = lire(f"{self.a.url}/metriques", delai=3)
            ligne = {"t": round(self.t(), 1), "service": "ok" if m else "injoignable"}
            if m:
                ligne.update({k: m[k] for k in (
                    "dossiers", "termines", "exec_en_cours", "exec_reussies", "exec_echouees",
                    "exec_interrompues", "etapes_reprises", "echecs_etapes", "doublons",
                    "depots_en_entree")})
            self.suivi.append(ligne)
            self.arret.wait(5)

    # --- moteur de secours (sans n8n) -----------------------------------------
    def moteur_simulateur(self) -> None:
        """Remplace n8n pour tester la campagne : un passage toutes les 10 s."""
        while not self.arret.is_set():
            subprocess.run([sys.executable, str(RACINE / "scripts" / "simuler_flux.py"),
                            "--url", self.a.url, "--tentatives", "3", "--delai", "2"],
                           capture_output=True)
            self.arret.wait(10 * self.a.echelle)

    # --- dépôts ---------------------------------------------------------------
    def vague(self, numero: int, nombre: int, marqueur: bool = True) -> list[str]:
        lot = f"CAMP-{self.nom}-V{numero}"
        cmd = [sys.executable, str(RACINE / "scripts" / "generer_dossiers.py"),
               "-n", str(nombre), "--lot", lot, "--graine", str(self.a.graine + numero),
               "--taux-erreurs", str(self.a.taux_erreurs),
               "--sortie", str(self.a.donnees / "entree")]
        if not marqueur:
            cmd.append("--sans-marqueur")
        subprocess.run(cmd, check=True, capture_output=True)
        refs = [l["reference"] for l in csv.DictReader(self.verite.open(encoding="utf-8"))
                if l["reference"].startswith(f"DEP-{lot}-")]
        maintenant = datetime.now().astimezone()
        if marqueur:
            for r in refs:
                self.depots[r] = maintenant
        self.journal(f"Vague {numero} : {len(refs)} dossiers déposés"
                     + ("" if marqueur else " (copie en cours, sans marqueur .pret)"))
        return refs

    def poser_marqueurs(self, refs: list[str]) -> None:
        maintenant = datetime.now().astimezone()
        for r in refs:
            (self.a.donnees / "entree" / r / ".pret").touch()
            self.depots[r] = maintenant
        self.journal(f"Copie terminée : marqueur .pret posé sur {len(refs)} dossiers")

    # --- pannes ---------------------------------------------------------------
    def panne(self, nom: str, description: str, debut, fin, duree: float) -> None:
        self.journal(f"PANNE {nom} : {description}")
        ev = {"panne": nom, "description": description, "debut": self.t()}
        ok = debut()
        time.sleep(duree * self.a.echelle)
        ok = fin() and ok
        ev["fin"] = self.t()
        ev["commandes_ok"] = ok
        self.evenements.append(ev)
        self.journal(f"FIN PANNE {nom} ({ev['fin'] - ev['debut']:.0f} s)")

    def attendre_traitement(self, max_s: float = 150) -> bool:
        """Attend qu'une exécution soit en cours, pour frapper « en plein traitement »."""
        limite = time.monotonic() + max_s
        while time.monotonic() < limite:
            m = lire(f"{self.a.url}/metriques", delai=2)
            if m and m["exec_en_cours"] > 0:
                return True
            time.sleep(0.5)
        self.journal("  aucun traitement en cours détecté : arrêt brutal déclenché quand même")
        return False

    def arret_brutal(self, service: str, libelle: str, redemarrage: float) -> None:
        en_cours = self.attendre_traitement()
        self.panne(f"ARRET_BRUTAL_{service.upper()}",
                   f"kill -9 {libelle}" + (" pendant un traitement" if en_cours else ""),
                   lambda: self.compose("kill", service),
                   lambda: self.compose("start", service), redemarrage)

    # --- scénario -----------------------------------------------------------
    def panne_avec_vague(self, nom: str, description: str, debut, fin, duree: float,
                         numero: int, taille: int) -> None:
        """Déclenche la panne, dépose une vague PENDANT la panne, puis rétablit.

        Les dossiers arrivent donc sur un système en panne : c'est ce qui
        oblige la chaîne à relancer, échouer proprement, puis reprendre.
        """
        self.journal(f"PANNE {nom} : {description}")
        ev = {"panne": nom, "description": description, "debut": self.t()}
        ok = debut()
        time.sleep(5 * self.a.echelle)
        refs = self.vague(numero, taille)
        ev["dossiers_exposes"] = len(refs)
        time.sleep(max(0.0, duree * self.a.echelle - (self.t() - ev["debut"])))
        ok = fin() and ok
        ev["fin"] = self.t()
        ev["commandes_ok"] = ok
        self.evenements.append(ev)
        self.journal(f"FIN PANNE {nom} ({ev['fin'] - ev['debut']:.0f} s)")

    def attendre_calme(self, max_s: float = 240) -> None:
        """Attend que les dossiers en entrée soient pris en charge avant la panne suivante."""
        limite = time.monotonic() + max_s * self.a.echelle
        while time.monotonic() < limite and not self.arret.is_set():
            m = lire(f"{self.a.url}/metriques", delai=3)
            if m and m["depots_en_entree"] == 0 and m["exec_en_cours"] == 0:
                return
            time.sleep(2)

    def derouler(self) -> None:
        n = self.a.nombre
        tailles = [n // 6] * 5 + [n - 5 * (n // 6)]

        self.vague(1, tailles[0])
        self.attendre_calme()
        if self.a.sans_pannes:
            for i in range(2, 7):
                self.vague(i, tailles[i - 1])
                self.attendre_calme()
            return

        self.panne_avec_vague("SMTP", "serveur mail arrêté",
                              lambda: self.compose("stop", "mailpit"),
                              lambda: self.compose("start", "mailpit"), 90, 2, tailles[1])
        self.attendre_calme()
        self.panne_avec_vague("SERVICE", "service de traitement arrêté",
                              lambda: self.compose("stop", "extracteur"),
                              lambda: self.compose("start", "extracteur"), 60, 3, tailles[2])
        self.attendre_calme()
        self.panne_avec_vague("RESEAU", "service déconnecté du réseau Docker",
                              lambda: self.docker("network", "disconnect", self.a.reseau, self.a.conteneur),
                              lambda: self.docker("network", "connect", "--alias", "extracteur",
                                                  self.a.reseau, self.a.conteneur), 60, 4, tailles[3])
        self.attendre_calme()

        # Dépôts encore en cours de copie : rien ne doit être traité avant le marqueur
        refs5 = self.vague(5, tailles[4], marqueur=False)
        time.sleep(40 * self.a.echelle)
        self.marqueurs_poses = datetime.now().astimezone()
        self.refs_copie = refs5
        self.poser_marqueurs(refs5)
        self.attendre_calme()

        self.vague(6, tailles[5])
        self.arret_brutal("extracteur", "du service de traitement", 10)
        self.arret_brutal("postgres", "de la base PostgreSQL", 15)
        if self.a.moteur == "n8n":
            self.arret_brutal("n8n", "de l'orchestrateur n8n", 15)

    def attendre_fin(self) -> datetime | None:
        self.journal("Attente de la fin du traitement de tous les dossiers...")
        limite = time.monotonic() + self.a.delai_max
        while time.monotonic() < limite:
            bilan = lire(f"{self.a.url}/bilan", delai=15)
            if bilan:
                fins = {d["reference"]: d["termine_le"] for d in bilan["dossiers"]
                        if d["reference"] in self.depots}
                restants = [r for r in self.depots if not fins.get(r)]
                if not restants:
                    derniere = max(horodate(v) for v in fins.values())
                    self.journal(f"Tous les dossiers sont terminés ({len(fins)}).")
                    return derniere
                self.journal(f"  encore {len(restants)} dossier(s) en cours ou en attente de reprise")
            time.sleep(15)
        self.journal("Délai maximal atteint : certains dossiers ne sont pas terminés.")
        return None

    # --- mesures et rapport ------------------------------------------------------
    def retablissement(self, ev: dict) -> float | None:
        """Secondes entre la fin d'une panne et la première exécution réussie suivante."""
        avant = [s for s in self.suivi if s["t"] <= ev["fin"] and "exec_reussies" in s]
        if not avant:
            return None
        reference = avant[-1]["exec_reussies"]
        for s in self.suivi:
            if s["t"] > ev["fin"] and s.get("exec_reussies", -1) > reference:
                return round(s["t"] - ev["fin"], 1)
        return None

    def rapport(self, derniere_fin: datetime | None, initial: dict, final: dict) -> Path:
        refs = set(self.depots)
        v = verifier(self.a.url, self.a.mailpit, self.verite, refs)
        latences = sorted(
            (horodate(d["termine_le"]) - self.depots[r]).total_seconds()
            for r, d in v["dossiers"].items() if d.get("termine_le"))
        duree_totale = (derniere_fin - self.debut).total_seconds() if derniere_fin else None
        delta = lambda k: final.get(k, 0) - initial.get(k, 0)
        termines = len(latences)
        manuel_min = len(refs) * self.a.minutes_manuelles

        dossier = RACINE / "docs" / "resultats"
        dossier.mkdir(parents=True, exist_ok=True)
        chemin_csv = dossier / f"campagne-{self.nom}-suivi.csv"
        cles = sorted({k for s in self.suivi for k in s}, key=lambda k: (k != "t", k))
        with chemin_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cles)
            w.writeheader()
            w.writerows(self.suivi)

        pct = lambda a, b: f"{100 * a / b:.1f} %" if b else "—"
        fmt_s = lambda s: "—" if s is None else (f"{s:.1f} s" if s < 120 else f"{s / 60:.1f} min")
        reprises_etapes = {e["etape"]: e["reprises"] for e in final.get("etapes", [])}
        reprises_init = {e["etape"]: e["reprises"] for e in initial.get("etapes", [])}

        c = v["courriels"] or {}
        refs_copie = getattr(self, "refs_copie", [])
        prematures = [r for r in refs_copie if r in v["dossiers"] and v["dossiers"][r].get("recu_le")
                      and horodate(v["dossiers"][r]["recu_le"]) < self.marqueurs_poses]
        lignes = [
            f"# Campagne de pannes du {self.debut:%d/%m/%Y à %H:%M}",
            "",
            "## Résultat principal",
            "",
            "| Indicateur | Valeur |",
            "|---|---|",
            f"| Dossiers déposés | {len(refs)} |",
            f"| Dossiers terminés (reprises comprises) | {termines} ({pct(termines, len(refs))}) |",
            f"| Décisions conformes au résultat attendu | {v['conformes']} / {v['traites']} ({pct(v['conformes'], v['traites'])}) |",
            f"| Doublons en base | {len(v['doublons_base'])} |",
            f"| E-mails envoyés en double | {c.get('doublons', 'non vérifié')} |",
            f"| Exécutions : réussies / échouées / interrompues | {delta('exec_reussies')} / {delta('exec_echouees')} / {delta('exec_interrompues')} |",
            f"| Taux de réussite des exécutions au premier coup | {pct(delta('exec_reussies'), delta('exec_reussies') + delta('exec_echouees') + delta('exec_interrompues'))} |",
            f"| Étapes reprises après un échec | {delta('etapes_reprises')} |",
            f"| Échecs techniques absorbés | {delta('echecs_etapes')} |",
            f"| Dépôts traités avant la fin de leur copie | "
            f"{len(prematures) if refs_copie else '—'}{f' sur {len(refs_copie)}' if refs_copie else ''} |",
            "",
            "## Pannes provoquées",
            "",
            "| Panne | Description | Début | Durée | Dossiers déposés pendant la panne | Rétablissement |",
            "|---|---|---|---|---|---|",
        ]
        for ev in self.evenements:
            lignes.append(f"| {ev['panne']} | {ev['description']} | {ev['debut']:.0f} s "
                          f"| {ev['fin'] - ev['debut']:.0f} s | {ev.get('dossiers_exposes', '—')} "
                          f"| {fmt_s(self.retablissement(ev))} |")
        if not self.evenements:
            lignes.append("| — | campagne de référence, sans panne | | | | |")
        lignes += [
            "",
            "*Rétablissement : délai entre la fin de la panne et la première exécution "
            "réussie suivante. Il dépend aussi de la planification n8n (un passage par minute) "
            "et du délai de verrou pour les exécutions tuées.*",
            "",
            "## Temps de traitement",
            "",
            "| Mesure | Valeur |",
            "|---|---|",
            f"| Durée totale de la campagne (1er dépôt → dernier dossier terminé) | {fmt_s(duree_totale)} |",
            f"| Délai médian dépôt → dossier terminé | {fmt_s(statistics.median(latences) if latences else None)} |",
            f"| Délai au 95e centile | {fmt_s(latences[int(0.95 * (len(latences) - 1))] if latences else None)} |",
            f"| Délai maximal (dossiers touchés par une panne) | {fmt_s(latences[-1] if latences else None)} |",
            f"| Équivalent manuel estimé ({self.a.minutes_manuelles:g} min par dossier) | {manuel_min / 60:.1f} h |",
            "",
            "*L'équivalent manuel est une hypothèse : ouvrir les pièces, vérifier, saisir, "
            "attribuer le matricule, rédiger l'e-mail, classer. À ajuster avec une mesure réelle.*",
            "",
            "## Reprises par étape",
            "",
            "| Étape | Étapes reprises pendant la campagne |",
            "|---|---|",
        ] + [f"| {e} | {n - reprises_init.get(e, 0)} |" for e, n in reprises_etapes.items()] + [
            "",
            "## Décisions",
            "",
            ", ".join(f"{k} : {n}" for k, n in v["decisions"].items()),
            "",
        ]
        if v["erreurs"]:
            lignes += ["## Décisions non conformes", ""] + [f"- {e}" for e in v["erreurs"]] + [""]
        lignes += [
            "## Paramètres",
            "",
            f"- Moteur : {self.a.moteur}",
            f"- Dossiers : {self.a.nombre}, part avec erreur volontaire : {self.a.taux_erreurs:.0%}",
            f"- Délai de verrou : {initial.get('duree_verrou_minutes', '?')} min",
            f"- Suivi détaillé (toutes les 5 s) : `{chemin_csv.name}`",
        ]
        chemin_md = dossier / f"campagne-{self.nom}.md"
        chemin_md.write_text("\n".join(lignes) + "\n", encoding="utf-8")
        return chemin_md


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", "--nombre", type=int, default=300, help="dossiers déposés (défaut : 300)")
    p.add_argument("--moteur", choices=["n8n", "simulateur"], default="n8n",
                   help="qui traite les dossiers : le flux n8n publié, ou le simulateur Python")
    p.add_argument("--sans-pannes", action="store_true", help="campagne de référence, sans panne")
    p.add_argument("--taux-erreurs", type=float, default=0.35)
    p.add_argument("--graine", type=int, default=2026)
    p.add_argument("--minutes-manuelles", type=float, default=6,
                   help="temps estimé pour traiter un dossier à la main (défaut : 6 min)")
    p.add_argument("--echelle", type=float, default=1.0, help="accélère (<1) ou ralentit (>1) le scénario")
    p.add_argument("--delai-max", type=float, default=1800, help="attente maximale de la fin (s)")
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--mailpit", default="http://localhost:8025")
    p.add_argument("--donnees", type=Path, default=RACINE / "data", help="dossier de données partagé avec le service")
    p.add_argument("--compose", default="docker compose")
    p.add_argument("--docker", default="docker")
    p.add_argument("--reseau", default="ap-reseau")
    p.add_argument("--conteneur", default="ap-extracteur")
    args = p.parse_args()

    initial = lire(f"{args.url}/metriques")
    if not initial:
        raise SystemExit(f"Service injoignable sur {args.url} : lance d'abord la pile (make demarrer).")
    if initial["duree_verrou_minutes"] > 5 and not args.sans_pannes:
        print(f"Attention : DUREE_VERROU_MINUTES = {initial['duree_verrou_minutes']}. Les dossiers "
              "interrompus par un arrêt brutal attendront ce délai avant reprise.\n"
              "Conseil : DUREE_VERROU_MINUTES=2 dans .env, puis make demarrer.\n")
    if initial["dossiers"]:
        print(f"Note : la base contient déjà {initial['dossiers']} dossiers. "
              "Pour des mesures nettes : make vider.\n")

    camp = Campagne(args)
    fils = [threading.Thread(target=camp.surveiller, daemon=True)]
    if args.moteur == "simulateur":
        fils.append(threading.Thread(target=camp.moteur_simulateur, daemon=True))
    for f in fils:
        f.start()

    try:
        camp.derouler()
        derniere = camp.attendre_fin()
    except KeyboardInterrupt:
        camp.journal("Interrompu : remise en marche des services avant de quitter...")
        derniere = None
        for s in ("mailpit", "extracteur", "postgres", "n8n"):
            camp.compose("start", s)
        camp.docker("network", "connect", "--alias", "extracteur", args.reseau, args.conteneur)
    finally:
        time.sleep(6)
        camp.arret.set()

    final = lire(f"{args.url}/metriques") or {}
    chemin = camp.rapport(derniere, initial, final)
    print("\n" + chemin.read_text(encoding="utf-8"))
    print(f"Rapport écrit dans {chemin.relative_to(RACINE)}")


if __name__ == "__main__":
    main()
