"""Conversations et messages en base (phase 5.3), et prompt envoyé au modèle.

Toutes les fonctions reçoivent la connexion de l'appelant : la Row-Level Security ne lui
montre que ses propres conversations.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.retrieval import Passage

SYSTEM_PROMPT = """Tu es l'assistant documentaire d'une entreprise. Tu réponds en français, \
uniquement à partir des extraits de documents fournis ci-dessous.

Règles :
- Appuie chaque affirmation sur un extrait et cite-le par son numéro entre crochets, \
par exemple [1] ou [2][3].
- Si les extraits ne permettent pas de répondre, dis-le clairement ; n'invente rien et \
n'utilise pas tes connaissances générales.
- Sois précis et concis."""

NO_PASSAGE_ANSWER = (
    "Je n'ai trouvé aucun passage pertinent dans les documents auxquels vous avez accès "
    "pour répondre à cette question."
)


@dataclass(frozen=True)
class Conversation:
    id: int
    title: str
    messages: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    content: str
    model: str | None
    citations: list[dict[str, Any]] | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    created_at: datetime


class UnknownConversation(Exception):
    pass


_CONVERSATIONS = """
SELECT c.id, c.title, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id)::int
       AS messages, c.created_at, c.updated_at
FROM conversations c
"""


async def list_conversations(conn: psycopg.AsyncConnection) -> list[Conversation]:
    async with conn.cursor(row_factory=class_row(Conversation)) as cur:
        await cur.execute(_CONVERSATIONS + " ORDER BY c.updated_at DESC, c.id DESC")
        return await cur.fetchall()


async def get_conversation(conn: psycopg.AsyncConnection, conversation_id: int) -> Conversation:
    async with conn.cursor(row_factory=class_row(Conversation)) as cur:
        await cur.execute(_CONVERSATIONS + " WHERE c.id = %s", (conversation_id,))
        found = await cur.fetchone()
    if found is None:  # inexistante, ou celle d'un autre : même réponse
        raise UnknownConversation(conversation_id)
    return found


async def create_conversation(conn: psycopg.AsyncConnection, user_id: int, title: str) -> int:
    cur = await conn.execute(
        "INSERT INTO conversations (user_id, title) VALUES (%s, %s) RETURNING id",
        (user_id, title[:80]),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def get_messages(
    conn: psycopg.AsyncConnection, conversation_id: int, *, last: int | None = None
) -> list[Message]:
    """Messages dans l'ordre ; last : seulement les derniers (historique envoyé au modèle)."""
    async with conn.cursor(row_factory=class_row(Message)) as cur:
        await cur.execute(
            "SELECT * FROM (SELECT id, role, content, model, citations, prompt_tokens,"
            " completion_tokens, latency_ms, created_at FROM messages"
            " WHERE conversation_id = %s ORDER BY id DESC LIMIT %s) m ORDER BY id",
            (conversation_id, last),
        )
        return await cur.fetchall()


async def add_message(
    conn: psycopg.AsyncConnection,
    conversation_id: int,
    role: str,
    content: str,
    *,
    model: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    latency_ms: int | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO messages (conversation_id, role, content, model, citations, prompt_tokens,"
        " completion_tokens, latency_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            conversation_id,
            role,
            content,
            model,
            Jsonb(citations) if citations is not None else None,
            prompt_tokens,
            completion_tokens,
            latency_ms,
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    await conn.execute(
        "UPDATE conversations SET updated_at = now() WHERE id = %s", (conversation_id,)
    )
    return row[0]


async def delete_conversation(conn: psycopg.AsyncConnection, conversation_id: int) -> bool:
    cur = await conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))
    return cur.rowcount == 1


def citations_of(passages: list[Passage]) -> list[dict[str, Any]]:
    """Sources numérotées comme dans le prompt ([1], [2]…), renvoyées au client et gardées."""
    return [
        {
            "n": n,
            "chunk_id": p.chunk_id,
            "document_id": p.document_id,
            "filename": p.filename,
            "collection": p.collection,
            "page": p.page,
            "score": round(p.score, 5),
            "excerpt": p.content[:300],
        }
        for n, p in enumerate(passages, start=1)
    ]


def build_messages(
    question: str, passages: list[Passage], history: list[Message]
) -> list[dict[str, str]]:
    """Prompt : consignes et extraits numérotés, historique récent, puis la question."""
    extracts = "\n\n".join(
        f"[{n}] {p.filename}, page {p.page}\n{p.content}" for n, p in enumerate(passages, start=1)
    )
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nExtraits :\n\n{extracts}"}]
    messages += [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": question})
    return messages
