"""Les 8 étapes du traitement d'un dossier.

Contrat commun à toutes les étapes (voir executer_etape) :

1. La base décide si l'étape doit tourner (pilotage.debuter_etape).
   Une étape déjà terminée est SAUTÉE et son résultat est renvoyé :
   c'est ce qui permet la reprise partielle.
2. Le travail de l'étape et son passage à « terminée » sont faits dans la
   MÊME transaction : on ne peut pas avoir l'un sans l'autre.
3. Une panne technique (base, disque, SMTP...) marque l'étape « échouée »
   et remonte une erreur 503 : l'orchestrateur relance plus tard.
4. Un problème métier (pièce illisible, reçu réutilisé...) n'est PAS une
   panne : c'est une décision, enregistrée comme un résultat normal.

Les effets de bord hors base (fichiers, e-mails) ne peuvent pas être
annulés par un rollback : ils sont conçus pour être rejouables sans
dommage (écriture atomique, déplacement reprenable, clé d'idempotence).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import smtplib
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from .config import CONFIG
from .db import connexion
from .extraction import Fichier, PieceIllisible, extraire, inventorier
from .pdf_simple import construire_pdf, ecrire_atomique

log = logging.getLogger("etapes")

REFERENCE_VALIDE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ORDRE = ["reception", "extraction", "controle", "enregistrement",
         "matricule", "accuse", "notification", "archivage"]


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------
class Introuvable(Exception):
    """404 : dossier ou dépôt inconnu."""


class Conflit(Exception):
    """409 : l'appel n'est pas permis dans l'état actuel (ordre, exécution concurrente)."""


class EchecEtape(Exception):
    """503 : panne technique, l'étape peut être relancée."""


class Ignorer(Exception):
    """L'étape ne s'applique pas à ce dossier (ex. pas de matricule si incomplet)."""


@dataclass
class Contexte:
    conn: psycopg.Connection
    dossier: dict
    execution_id: int

    @property
    def reference(self) -> str:
        return self.dossier["reference"]

    @property
    def depot(self) -> Path:
        return CONFIG.entree / self.reference

    def resultat(self, etape: str) -> dict:
        ligne = self.conn.execute(
            "SELECT resultat FROM pilotage.etapes_dossier WHERE dossier_id = %s AND etape = %s",
            (self.dossier["id"], etape)).fetchone()
        return (ligne or {}).get("resultat") or {}

    def recharger(self) -> dict:
        self.dossier = self.conn.execute(
            "SELECT * FROM inscription.dossiers WHERE id = %s", (self.dossier["id"],)).fetchone()
        return self.dossier


def verifier_reference(reference: str) -> None:
    # Empêche toute sortie du dossier de données (« ../../etc »)
    if not REFERENCE_VALIDE.match(reference):
        raise Introuvable(f"Référence invalide : {reference!r}")


# ---------------------------------------------------------------------------
# 1. Réception
# ---------------------------------------------------------------------------
def reception(ctx: Contexte) -> dict:
    if not ctx.depot.is_dir():
        raise EchecEtape(f"Dépôt introuvable : {ctx.depot}")
    if not (ctx.depot / ".pret").exists():
        raise EchecEtape("Dépôt en cours de copie (marqueur .pret absent)")

    inv = inventorier(ctx.depot)
    for f in inv.fichiers.values():
        # Une nouvelle version d'une pièce remplace l'ancienne comme « courante »
        ctx.conn.execute(
            """UPDATE inscription.pieces SET courante = false
                WHERE dossier_id = %s AND type_piece = %s AND empreinte_sha256 <> %s""",
            (ctx.dossier["id"], f.type_piece, f.empreinte))
        ctx.conn.execute(
            """INSERT INTO inscription.pieces
                   (dossier_id, type_piece, nom_fichier, empreinte_sha256, taille_octets)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (dossier_id, type_piece, empreinte_sha256)
               DO UPDATE SET courante = true, nom_fichier = EXCLUDED.nom_fichier""",
            (ctx.dossier["id"], f.type_piece, f.chemin.name, f.empreinte, f.taille))

    obligatoires = [l["code"] for l in ctx.conn.execute(
        "SELECT code FROM inscription.types_piece WHERE obligatoire ORDER BY code")]
    return {
        "pieces": {t: {"fichier": f.chemin.name, "sha256": f.empreinte[:16], "octets": f.taille}
                   for t, f in inv.fichiers.items()},
        "absentes": [t for t in obligatoires if t not in inv.fichiers],
        "fichiers_inconnus": inv.inconnus,
    }


# ---------------------------------------------------------------------------
# 2. Extraction
# ---------------------------------------------------------------------------
def extraction(ctx: Contexte) -> dict:
    pieces = ctx.conn.execute(
        """SELECT id, type_piece, nom_fichier, empreinte_sha256, taille_octets
             FROM inscription.pieces WHERE dossier_id = %s AND courante ORDER BY type_piece""",
        (ctx.dossier["id"],)).fetchall()

    bilan = {}
    for p in pieces:
        chemin = ctx.depot / p["nom_fichier"]
        if not chemin.is_file():
            raise EchecEtape(f"Pièce disparue du dépôt : {p['nom_fichier']}")
        try:
            donnees = extraire(Fichier(p["type_piece"], chemin, p["empreinte_sha256"], p["taille_octets"]))
            ctx.conn.execute(
                """UPDATE inscription.pieces
                      SET controle = 'valide', donnees_extraites = %s, erreur = NULL WHERE id = %s""",
                (Jsonb(donnees), p["id"]))
            bilan[p["type_piece"]] = "valide"
        except PieceIllisible as e:
            # Décision métier, pas une panne : la pièce est marquée invalide
            ctx.conn.execute(
                """UPDATE inscription.pieces
                      SET controle = 'invalide', donnees_extraites = NULL, erreur = %s WHERE id = %s""",
                (str(e), p["id"]))
            bilan[p["type_piece"]] = f"invalide : {e}"
    return {"pieces": bilan}


# ---------------------------------------------------------------------------
# 3. Contrôle : complétude et cohérence. Décide, n'écrit rien.
# ---------------------------------------------------------------------------
def _date_fr(texte: str | None) -> date | None:
    try:
        return datetime.strptime((texte or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def _montant(texte: str | None) -> int | None:
    chiffres = re.sub(r"\D", "", (texte or "").split("Ar")[0])
    return int(chiffres) if chiffres else None


def _cle(ctx: Contexte, nom: str, prenoms: str, naissance: date) -> str:
    return ctx.conn.execute("SELECT inscription.cle_identite(%s, %s, %s) AS c",
                            (nom, prenoms, naissance)).fetchone()["c"]


def controle(ctx: Contexte) -> dict:
    lignes = ctx.conn.execute(
        """SELECT type_piece, controle, donnees_extraites, erreur
             FROM inscription.pieces WHERE dossier_id = %s AND courante""",
        (ctx.dossier["id"],)).fetchall()
    pieces = {l["type_piece"]: l for l in lignes}
    valides = {t: l["donnees_extraites"] for t, l in pieces.items() if l["controle"] == "valide"}
    obligatoires = [l["code"] for l in ctx.conn.execute(
        "SELECT code FROM inscription.types_piece WHERE obligatoire ORDER BY code")]

    manquantes: list[dict] = []
    rejets: list[str] = []
    for t in obligatoires:
        if t not in pieces:
            manquantes.append({"piece": t, "raison": "absente"})
        elif t not in valides:
            manquantes.append({"piece": t, "raison": f"illisible ({pieces[t]['erreur']})"})

    identite, recu = None, None
    f = valides.get("formulaire")
    if f:
        naissance = _date_fr(f.get("date_de_naissance"))
        if naissance is None:
            manquantes.append({"piece": "formulaire", "raison": "date de naissance invalide"})
        else:
            identite = {"nom": f["nom"].strip(), "prenoms": f["prenoms"].strip(),
                        "date_naissance": naissance.isoformat(), "email": f["email"].strip(),
                        "telephone": (f.get("telephone") or "").strip() or None,
                        "parcours": f["parcours_demande"].strip().upper()}
            cle = _cle(ctx, identite["nom"], identite["prenoms"], naissance)
            identite["cle"] = cle

            r = valides.get("releve_bac")
            if r:
                if _cle(ctx, r["nom"], r["prenoms"], _date_fr(r.get("date_de_naissance")) or date.min) != cle:
                    rejets.append("identité différente entre le formulaire et le relevé de notes")
            a = valides.get("attestation")
            if a:
                if _cle(ctx, a["nom"], a["prenoms"], date.min) != _cle(ctx, identite["nom"], identite["prenoms"], date.min):
                    rejets.append("identité différente entre le formulaire et l'attestation")

            if not ctx.conn.execute("SELECT 1 FROM inscription.parcours WHERE code = %s",
                                    (identite["parcours"],)).fetchone():
                rejets.append(f"parcours inconnu : {identite['parcours']}")

            deja = ctx.conn.execute(
                """SELECT d.reference FROM inscription.dossiers d
                     JOIN inscription.candidats c ON c.id = d.candidat_id
                    WHERE c.cle_identite = %s AND d.id <> %s
                      AND d.annee_universitaire = %s AND d.statut = 'complet'""",
                (cle, ctx.dossier["id"], ctx.dossier["annee_universitaire"])).fetchone()
            if deja:
                rejets.append(f"candidat déjà inscrit cette année (dossier {deja['reference']})")

    rc = valides.get("recu")
    if rc:
        montant = _montant(rc.get("montant"))
        paiement = _date_fr(rc.get("date_de_paiement"))
        recu = {"numero": rc["numero_de_recu"].strip(), "montant": montant,
                "date": paiement.isoformat() if paiement else None}
        if montant != CONFIG.droits_ar:
            manquantes.append({"piece": "recu", "raison":
                               f"montant {montant} Ar au lieu de {CONFIG.droits_ar} Ar"})
        elif paiement is None:
            manquantes.append({"piece": "recu", "raison": "date de paiement invalide"})
        autre = ctx.conn.execute(
            """SELECT d.reference FROM inscription.recus_paiement r
                 JOIN inscription.dossiers d ON d.id = r.dossier_id
                WHERE r.numero = %s AND r.dossier_id <> %s""",
            (recu["numero"], ctx.dossier["id"])).fetchone()
        if autre:
            rejets.append(f"reçu {recu['numero']} déjà utilisé (dossier {autre['reference']})")

    decision = "rejete" if rejets else "incomplet" if manquantes else "complet"
    return {"decision": decision, "manquantes": manquantes, "motifs_rejet": rejets,
            "identite": identite, "recu": recu}


# ---------------------------------------------------------------------------
# 4. Enregistrement : la base est l'arbitre final
# ---------------------------------------------------------------------------
def enregistrement(ctx: Contexte) -> dict:
    ctrl = ctx.resultat("controle")
    decision, motifs = ctrl["decision"], list(ctrl["motifs_rejet"])
    ident, recu = ctrl.get("identite"), ctrl.get("recu")
    candidat_id = None

    if ident:
        # ON CONFLICT DO UPDATE verrouille la ligne du candidat jusqu'au commit :
        # deux dossiers du même candidat traités en même temps sont sérialisés ici.
        candidat_id = ctx.conn.execute(
            """INSERT INTO inscription.candidats
                   (nom, prenoms, date_naissance, email, telephone, cle_identite)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (cle_identite)
               DO UPDATE SET email = EXCLUDED.email, telephone = EXCLUDED.telephone
               RETURNING id""",
            (ident["nom"], ident["prenoms"], ident["date_naissance"], ident["email"],
             ident["telephone"], ident["cle"])).fetchone()["id"]

        if decision == "complet":
            deja = ctx.conn.execute(
                """SELECT reference FROM inscription.dossiers
                    WHERE candidat_id = %s AND id <> %s AND statut = 'complet'
                      AND annee_universitaire = %s""",
                (candidat_id, ctx.dossier["id"], ctx.dossier["annee_universitaire"])).fetchone()
            if deja:
                decision = "rejete"
                motifs.append(f"candidat déjà inscrit cette année (dossier {deja['reference']})")

    if decision == "complet" and recu:
        ctx.conn.execute("DELETE FROM inscription.recus_paiement WHERE dossier_id = %s AND numero <> %s",
                         (ctx.dossier["id"], recu["numero"]))
        ctx.conn.execute(
            """INSERT INTO inscription.recus_paiement (numero, dossier_id, montant_ar, date_paiement)
               VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (recu["numero"], ctx.dossier["id"], recu["montant"], recu["date"]))
        proprietaire = ctx.conn.execute(
            "SELECT dossier_id FROM inscription.recus_paiement WHERE numero = %s",
            (recu["numero"],)).fetchone()
        if proprietaire["dossier_id"] != ctx.dossier["id"]:
            # Un autre dossier a enregistré ce reçu entre notre contrôle et maintenant
            decision = "rejete"
            motifs.append(f"reçu {recu['numero']} déjà utilisé par un autre dossier")

    motif = "; ".join(motifs) if motifs else (
        "; ".join(f"{m['piece']} : {m['raison']}" for m in ctrl["manquantes"]) or None)
    parcours = ident["parcours"] if ident and not any("parcours inconnu" in m for m in motifs) else None
    ctx.conn.execute(
        """UPDATE inscription.dossiers
              SET candidat_id = %s, parcours_code = %s, statut = %s, motif = %s
            WHERE id = %s""",
        (candidat_id, parcours, decision, motif, ctx.dossier["id"]))
    return {"statut": decision, "candidat_id": candidat_id, "motif": motif}


# ---------------------------------------------------------------------------
# 5. Matricule
# ---------------------------------------------------------------------------
def matricule(ctx: Contexte) -> dict:
    d = ctx.recharger()
    if d["statut"] != "complet":
        raise Ignorer(f"dossier {d['statut']}")
    m = ctx.conn.execute("SELECT inscription.attribuer_matricule(%s) AS m", (d["id"],)).fetchone()["m"]
    return {"matricule": m}


# ---------------------------------------------------------------------------
# 6. Accusé de réception (PDF)
# ---------------------------------------------------------------------------
def chemin_accuse(d: dict) -> Path:
    return CONFIG.accuses / d["annee_universitaire"] / f"{d['matricule']}.pdf"


def accuse(ctx: Contexte) -> dict:
    d = ctx.recharger()
    if d["statut"] != "complet":
        raise Ignorer(f"dossier {d['statut']}")
    ident = ctx.resultat("controle")["identite"]
    libelle = ctx.conn.execute("SELECT libelle FROM inscription.parcours WHERE code = %s",
                               (d["parcours_code"],)).fetchone()["libelle"]
    contenu = construire_pdf("ACCUSÉ DE RÉCEPTION — INSCRIPTION EN PREMIÈRE ANNÉE", [
        f"Matricule : {d['matricule']}",
        f"Nom : {ident['nom']}",
        f"Prénoms : {ident['prenoms']}",
        f"Parcours : {d['parcours_code']} — {libelle}",
        f"Année universitaire : {d['annee_universitaire']}",
        f"Référence du dépôt : {d['reference']}",
        f"Dossier reçu le : {d['recu_le']:%d/%m/%Y}",
        "",
        "Votre dossier est complet et votre inscription est enregistrée.",
    ], "Université fictive — document généré automatiquement pour un projet de démonstration.")
    cible = chemin_accuse(d)
    ecrire_atomique(cible, contenu)   # rejouable : même contenu, écriture atomique
    return {"fichier": str(cible.relative_to(CONFIG.donnees)),
            "sha256": hashlib.sha256(contenu).hexdigest()[:16]}


# ---------------------------------------------------------------------------
# 7. Notification (e-mail)
# ---------------------------------------------------------------------------
def _message(ctx: Contexte, d: dict, ctrl: dict) -> tuple[str, str, str, str, Path | None]:
    """Renvoie (type, clé d'idempotence, sujet, corps, pièce jointe)."""
    ident = ctrl["identite"]
    salutation = f"Bonjour {ident['prenoms']} {ident['nom']},\n\n"
    signature = "\n\nLe service de la scolarité\n(message automatique — université fictive)"

    if d["statut"] == "complet":
        return ("accuse_reception", f"accuse_reception:{d['id']}",
                f"Inscription enregistrée — matricule {d['matricule']}",
                salutation + f"Votre dossier {d['reference']} est complet.\n"
                f"Votre matricule est {d['matricule']} (parcours {d['parcours_code']}).\n"
                "Vous trouverez l'accusé de réception en pièce jointe." + signature,
                chemin_accuse(d))

    if d["statut"] == "incomplet":
        liste = sorted(f"{m['piece']} : {m['raison']}" for m in ctrl["manquantes"])
        # La clé dépend de la liste : si le candidat corrige puis qu'il manque
        # autre chose, il reçoit un nouveau message ; sinon jamais deux fois le même.
        empreinte = hashlib.sha1(json.dumps(liste).encode()).hexdigest()[:10]
        return ("pieces_manquantes", f"pieces_manquantes:{d['id']}:{empreinte}",
                f"Dossier {d['reference']} incomplet",
                salutation + f"Votre dossier {d['reference']} est incomplet :\n"
                + "\n".join(f"  - {l}" for l in liste)
                + "\n\nMerci de déposer les pièces corrigées." + signature, None)

    return ("rejet", f"rejet:{d['id']}", f"Dossier {d['reference']} non recevable",
            salutation + f"Votre dossier {d['reference']} ne peut pas être accepté.\n"
            f"Motif : {d['motif']}" + signature, None)


def notification(ctx: Contexte) -> dict:
    d = ctx.recharger()
    ctrl = ctx.resultat("controle")
    if not ctrl.get("identite") or not ctrl["identite"].get("email"):
        raise Ignorer("aucune adresse e-mail exploitable (formulaire absent ou illisible)")

    type_msg, cle, sujet, corps, piece_jointe = _message(ctx, d, ctrl)
    destinataire = ctrl["identite"]["email"]

    # La réservation verrouille la clé jusqu'au commit : deux exécutions
    # simultanées ne peuvent pas envoyer le même message.
    a_envoyer = ctx.conn.execute(
        "SELECT pilotage.reserver_notification(%s, %s, %s, %s) AS ok",
        (cle, d["id"], type_msg, destinataire)).fetchone()["ok"]
    if not a_envoyer:
        return {"type": type_msg, "cle": cle, "envoye": False, "raison": "déjà envoyé"}

    msg = EmailMessage()
    msg["From"] = CONFIG.expediteur
    msg["To"] = destinataire
    msg["Subject"] = sujet
    msg["Message-ID"] = f"<{cle.replace(':', '.')}@admission-pipeline.local>"
    msg["X-Cle-Idempotence"] = cle
    msg.set_content(corps)
    if piece_jointe is not None:
        if not piece_jointe.is_file():
            raise EchecEtape(f"Accusé introuvable : {piece_jointe}")
        msg.add_attachment(piece_jointe.read_bytes(), maintype="application",
                           subtype="pdf", filename=piece_jointe.name)
    try:
        with smtplib.SMTP(CONFIG.smtp_hote, CONFIG.smtp_port, timeout=10) as smtp:
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise EchecEtape(f"Envoi SMTP impossible : {e}") from e

    ctx.conn.execute("SELECT pilotage.confirmer_notification(%s)", (cle,))
    return {"type": type_msg, "cle": cle, "destinataire": destinataire, "envoye": True}


# ---------------------------------------------------------------------------
# 8. Archivage : déplacement reprenable
# ---------------------------------------------------------------------------
def destination(d: dict) -> Path:
    if d["statut"] == "complet":
        return CONFIG.archives / d["annee_universitaire"] / d["parcours_code"] / d["matricule"]
    if d["statut"] == "incomplet":
        return CONFIG.en_attente / d["reference"]
    return CONFIG.rejets / d["reference"]


def archivage(ctx: Contexte) -> dict:
    d = ctx.recharger()
    cible = destination(d)
    if ctx.depot.is_dir():
        cible.mkdir(parents=True, exist_ok=True)
        # Fichier par fichier : si le traitement s'arrête au milieu, la
        # relance déplace simplement ce qui reste.
        for element in sorted(ctx.depot.iterdir()):
            if element.is_dir():
                shutil.move(str(element), str(cible / element.name))
            else:
                os.replace(element, cible / element.name)
        ctx.depot.rmdir()
    elif not cible.is_dir():
        raise EchecEtape(f"Dépôt introuvable à la source ({ctx.depot}) comme à la destination ({cible})")
    return {"destination": str(cible.relative_to(CONFIG.donnees))}


ETAPES: dict[str, Callable[[Contexte], dict]] = {
    "reception": reception, "extraction": extraction, "controle": controle,
    "enregistrement": enregistrement, "matricule": matricule, "accuse": accuse,
    "notification": notification, "archivage": archivage,
}


# ---------------------------------------------------------------------------
# Exécution d'une étape (le contrat décrit en tête de fichier)
# ---------------------------------------------------------------------------
def executer_etape(reference: str, etape: str, execution_id: int) -> dict:
    verifier_reference(reference)
    if etape not in ETAPES:
        raise Introuvable(f"Étape inconnue : {etape}")

    with connexion() as conn:
        dossier = conn.execute("SELECT * FROM inscription.dossiers WHERE reference = %s",
                               (reference,)).fetchone()
        if dossier is None:
            raise Introuvable(f"Dossier inconnu : {reference}")
        execution = conn.execute("SELECT * FROM pilotage.executions WHERE id = %s",
                                 (execution_id,)).fetchone()
        if execution is None or execution["dossier_id"] != dossier["id"]:
            raise Conflit("Exécution inconnue ou rattachée à un autre dossier")
        if execution["statut"] != "en_cours":
            raise Conflit(f"Exécution {execution_id} déjà close ({execution['statut']})")

        precedentes = ORDRE[:ORDRE.index(etape)]
        faites = {l["etape"] for l in conn.execute(
            """SELECT etape FROM pilotage.etapes_dossier
                WHERE dossier_id = %s AND statut IN ('terminee', 'ignoree')""", (dossier["id"],))}
        restantes = [e for e in precedentes if e not in faites]
        if restantes:
            raise Conflit(f"Étape précédente non terminée : {restantes[0]}")

        depart = conn.execute("SELECT * FROM pilotage.debuter_etape(%s, %s, %s)",
                              (dossier["id"], etape, execution_id)).fetchone()

    if not depart["a_faire"]:
        return {"etape": etape, "statut": "sautee", "tentative": depart["tentative"],
                "resultat": depart["resultat"]}

    try:
        with connexion() as conn:
            ctx = Contexte(conn, dossier, execution_id)
            resultat = ETAPES[etape](ctx)
            conn.execute("SELECT pilotage.terminer_etape(%s, %s, %s, %s)",
                         (dossier["id"], etape, execution_id, Jsonb(resultat)))
        return {"etape": etape, "statut": "terminee", "tentative": depart["tentative"],
                "resultat": resultat}

    except Ignorer as e:
        with connexion() as conn:
            conn.execute("SELECT pilotage.ignorer_etape(%s, %s, %s, %s)",
                         (dossier["id"], etape, execution_id, str(e)))
        return {"etape": etape, "statut": "ignoree", "tentative": depart["tentative"],
                "resultat": {"ignoree": True, "raison": str(e)}}

    except Exception as e:
        message = str(e) if isinstance(e, EchecEtape) else f"{type(e).__name__} : {e}"
        log.warning("Échec %s/%s (tentative %s) : %s", reference, etape, depart["tentative"], message)
        try:
            with connexion() as conn:
                conn.execute("SELECT pilotage.echouer_etape(%s, %s, %s, %s)",
                             (dossier["id"], etape, execution_id, message))
        except psycopg.Error:
            log.error("Impossible de journaliser l'échec (base injoignable)")
        raise EchecEtape(message) from e
