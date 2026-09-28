"""Écriture de PDF texte minimalistes, sans dépendance externe.

Suffisant pour produire les pièces fictives et les accusés de réception :
une page A4, police Helvetica, un titre et des lignes « Clé : valeur ».
Les caractères accentués sont encodés en WinAnsi (cp1252), ce que les
lecteurs PDF et pypdf décodent correctement.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _echapper(texte: str) -> bytes:
    brut = texte.encode("cp1252", errors="replace")
    return brut.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def construire_pdf(titre: str, lignes: list[str], pied: str | None = None) -> bytes:
    """Renvoie le contenu binaire d'un PDF d'une page."""
    flux = [b"BT", b"/F1 16 Tf", b"56 780 Td", b"(" + _echapper(titre) + b") Tj", b"/F1 11 Tf"]
    flux.append(b"0 -36 Td")
    for ligne in lignes:
        flux.append(b"(" + _echapper(ligne) + b") Tj")
        flux.append(b"0 -18 Td")
    flux.append(b"ET")
    if pied:
        flux += [b"BT", b"/F1 8 Tf", b"56 40 Td", b"(" + _echapper(pied) + b") Tj", b"ET"]
    contenu = b"\n".join(flux)

    objets = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(contenu)).encode() + b" >>\nstream\n" + contenu + b"\nendstream",
    ]

    sortie = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    positions = []
    for numero, objet in enumerate(objets, start=1):
        positions.append(len(sortie))
        sortie += f"{numero} 0 obj\n".encode() + objet + b"\nendobj\n"
    debut_xref = len(sortie)
    sortie += f"xref\n0 {len(objets) + 1}\n0000000000 65535 f \n".encode()
    for position in positions:
        sortie += f"{position:010d} 00000 n \n".encode()
    sortie += (
        f"trailer\n<< /Size {len(objets) + 1} /Root 1 0 R >>\n"
        f"startxref\n{debut_xref}\n%%EOF\n"
    ).encode()
    return bytes(sortie)


def ecrire_atomique(chemin: Path, contenu: bytes) -> None:
    """Écrit un fichier de façon atomique.

    On écrit d'abord dans un fichier temporaire du même dossier, puis on le
    renomme. Un arrêt brutal pendant l'écriture laisse soit l'ancien
    fichier, soit le nouveau, jamais un fichier à moitié écrit.
    """
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, temporaire = tempfile.mkstemp(dir=chemin.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenu)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporaire, 0o644)   # mkstemp crée en 0600
        os.replace(temporaire, chemin)
    except BaseException:
        Path(temporaire).unlink(missing_ok=True)
        raise
