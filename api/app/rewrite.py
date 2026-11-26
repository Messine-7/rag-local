"""Reformulation des questions de suivi (phase 5.4).

Dans une conversation, « Et pour 2025 ? » ou « Qui l'a signé ? » ne se comprennent qu'avec
les messages précédents : cherchés tels quels (embedding et plein texte), ils ramènent de
mauvais passages. Avant la recherche, le modèle réécrit donc la question pour qu'elle se
comprenne seule (« Quelle est la grille des salaires pour 2025 ? »).

La question reformulée ne sert qu'à la recherche : le modèle répond ensuite à la question
d'origine, avec l'historique. Elle est gardée avec la question (messages.search_query) pour
pouvoir juger la recherche. Si la reformulation échoue ou donne un texte inutilisable, la
recherche se fait sur la question d'origine : une reformulation ratée ne doit jamais faire
échouer une question.
"""

import logging
import re

from app.conversations import Message
from app.llm import LLMClient, LLMError

log = logging.getLogger(__name__)

REWRITE_PROMPT = """Tu reformules la dernière question d'un utilisateur pour qu'elle se \
comprenne seule, sans la conversation qui précède. Elle servira à chercher des passages \
dans des documents.

Règles :
- Remplace les pronoms et les allusions (« il », « ce document », « et pour 2025 ? ») par \
ce qu'ils désignent dans la conversation, sans ajouter d'autres détails.
- Garde la langue de la question, son sens et ses termes exacts (noms, sigles, chiffres).
- Si la question se comprend déjà seule, recopie-la telle quelle.
- Ne réponds pas à la question.
- Écris uniquement la question reformulée, sur une seule ligne, sans guillemets ni \
commentaire."""

# Réponses longues tronquées : seul leur sujet compte pour comprendre la question
MAX_MESSAGE_CHARS = 600
MAX_REWRITE_CHARS = 1000

_CITATION = re.compile(r"\s*(\[\d+\])+")
_LABEL = re.compile(r"^(question( reformulée)?|reformulation)\s*:\s*", re.IGNORECASE)
_QUOTES = "\"'«»“”‘’` "


def build_rewrite_messages(question: str, history: list[Message]) -> list[dict[str, str]]:
    lines = []
    for m in history:
        text = _CITATION.sub("", m.content).strip()
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS].rstrip() + "…"
        who = "Utilisateur" if m.role == "user" else "Assistant"
        lines.append(f"{who} : {text}")
    conversation = "\n".join(lines)
    return [
        {"role": "system", "content": REWRITE_PROMPT},
        {
            "role": "user",
            "content": f"Conversation :\n{conversation}\n\nDernière question : {question}",
        },
    ]


def clean_rewrite(text: str) -> str | None:
    """Première ligne non vide, sans étiquette ni guillemets ; None si inutilisable."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = _LABEL.sub("", line).strip(_QUOTES)
    if not line or len(line) > MAX_REWRITE_CHARS:
        return None
    return line


async def rewrite_question(
    llm: LLMClient,
    question: str,
    history: list[Message],
    *,
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
) -> str | None:
    """Question autonome pour la recherche ; None : chercher avec la question d'origine."""
    if not history:
        return None
    try:
        generation = await llm.chat(
            build_rewrite_messages(question, history),
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            reasoning_effort=reasoning_effort,
        )
    except LLMError as exc:
        log.warning("reformulation impossible, recherche sur la question d'origine : %s", exc)
        return None
    rewritten = clean_rewrite(generation.text)
    if rewritten is None:
        log.warning("reformulation inutilisable : %r", generation.text[:200])
    elif rewritten == question.strip():
        return None  # déjà autonome : rien de reformulé
    return rewritten
