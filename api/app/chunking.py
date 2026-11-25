"""Découpage du texte des PDF en chunks à vectoriser.

Découpage par taille (en caractères) avec chevauchement, en coupant de préférence entre
deux paragraphes, puis entre deux phrases, puis entre deux mots. Chaque chunk reste sur
une seule page, pour pouvoir citer la page dans les réponses (phase 5).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Après normalize(), il ne reste que des sauts de paragraphe ("\n\n") : plus de "\n" isolé
SEPARATORS = ("\n\n", ". ", " ")


@dataclass(frozen=True)
class Chunk:
    page: int  # numérotée à partir de 1
    content: str


def normalize(text: str) -> str:
    """Un paragraphe par bloc, séparés par une ligne vide ; espaces et retours à la ligne
    internes ramenés à une espace. Retire aussi les octets nuls, refusés par Postgres."""
    paragraphs = re.split(r"\n\s*\n", text.replace("\x00", ""))
    cleaned = (re.sub(r"\s+", " ", p).strip() for p in paragraphs)
    return "\n\n".join(p for p in cleaned if p)


def _split(text: str, max_chars: int, separators: Sequence[str]) -> list[str]:
    """Morceaux de max_chars au plus, coupés sur le premier séparateur présent."""
    if len(text) <= max_chars:
        return [text]
    for i, sep in enumerate(separators):
        if sep not in text:
            continue
        parts = text.split(sep)
        pieces = [p + sep for p in parts[:-1]] + [parts[-1]]  # le séparateur reste attaché
        out: list[str] = []
        for piece in pieces:
            if piece:
                out.extend(_split(piece, max_chars, separators[i + 1 :]))
        return out
    # Aucun séparateur (mot démesuré, tableau sans espaces…) : coupe franche
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _tail(text: str, overlap: int) -> str:
    """Fin du chunk précédent reprise au début du suivant, commençant sur un début de mot."""
    if overlap <= 0:
        return ""
    tail = text[-overlap:]
    if len(text) > overlap and " " in tail:
        tail = tail[tail.index(" ") + 1 :]
    return tail


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Chunks d'environ max_chars caractères (au plus max_chars + overlap)."""
    if max_chars <= 0 or not 0 <= overlap < max_chars:
        raise ValueError("il faut 0 <= overlap < max_chars")
    chunks: list[str] = []
    current = ""
    for piece in _split(normalize(text), max_chars, SEPARATORS):
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current.strip())
            current = _tail(current, overlap)
        current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks


def chunk_pages(pages: Sequence[str], max_chars: int, overlap: int) -> list[Chunk]:
    """Chunks de toutes les pages, dans l'ordre ; les pages sans texte n'en donnent aucun."""
    return [
        Chunk(page=number, content=content)
        for number, text in enumerate(pages, start=1)
        for content in split_text(text, max_chars, overlap)
    ]
