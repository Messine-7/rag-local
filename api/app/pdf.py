"""Extraction du texte des PDF avec PyMuPDF."""

from pathlib import Path

import pymupdf


class PdfError(Exception):
    """PDF illisible : réessayer ne servirait à rien."""


def extract_pages(path: Path) -> list[str]:
    """Texte de chaque page, un paragraphe (bloc PyMuPDF) par bloc séparé d'une ligne vide.

    Les PDF scannés (images sans couche texte) donnent des pages vides : pas d'OCR ici.
    """
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # PyMuPDF lève plusieurs types selon la corruption du fichier
        raise PdfError(f"PDF illisible : {exc}") from exc
    with doc:
        if doc.needs_pass:
            raise PdfError("PDF protégé par un mot de passe")
        pages = []
        for page in doc:
            # sort=True : ordre de lecture (haut en bas, gauche à droite)
            # block[6] : 0 = texte, 1 = image ; block[4] : le texte
            blocks = page.get_text("blocks", sort=True)
            pages.append("\n\n".join(block[4] for block in blocks if block[6] == 0))
        return pages
