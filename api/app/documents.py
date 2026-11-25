"""Documents et jobs d'ingestion en base, côté API."""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import class_row

DEFAULT_COLLECTION = "défaut"


class UnknownCollection(Exception):
    pass


class CollectionForbidden(Exception):
    """Collection visible (droit de lecture) mais pas de droit d'écriture."""


@dataclass(frozen=True)
class Document:
    id: int
    collection: str
    filename: str
    sha256: str
    size_bytes: int
    pages: int | None
    status: str
    error: str | None
    chunks: int
    uploaded_by: str | None  # NULL pour les documents envoyés avant la phase 5
    created_at: datetime
    updated_at: datetime


_SELECT = """
SELECT d.id, c.name AS collection, d.filename, d.sha256, d.size_bytes, d.pages, d.status,
       d.error, (SELECT count(*) FROM chunks ch WHERE ch.document_id = d.id)::int AS chunks,
       u.username AS uploaded_by, d.created_at, d.updated_at
FROM documents d
JOIN collections c ON c.id = d.collection_id
LEFT JOIN users u ON u.id = d.created_by
"""


async def get_document(conn: psycopg.AsyncConnection, document_id: int) -> Document | None:
    async with conn.cursor(row_factory=class_row(Document)) as cur:
        await cur.execute(_SELECT + " WHERE d.id = %s", (document_id,))
        return await cur.fetchone()


async def list_documents(conn: psycopg.AsyncConnection) -> list[Document]:
    async with conn.cursor(row_factory=class_row(Document)) as cur:
        await cur.execute(_SELECT + " ORDER BY d.id DESC")
        return await cur.fetchall()


@dataclass(frozen=True)
class Collection:
    id: int
    name: str
    permission: str  # droit de l'appelant : read ou write


async def list_collections(conn: psycopg.AsyncConnection) -> list[Collection]:
    """Collections que l'appelant peut lire (filtrées par la RLS), avec son droit sur chacune."""
    async with conn.cursor(row_factory=class_row(Collection)) as cur:
        await cur.execute(
            "SELECT c.id, c.name,"
            " CASE WHEN c.id = ANY ((SELECT rag_collections('write'))::bigint[])"
            " THEN 'write' ELSE 'read' END AS permission"
            " FROM collections c ORDER BY c.name"
        )
        return await cur.fetchall()


async def check_can_write(conn: psycopg.AsyncConnection, collection: str) -> None:
    """Avant d'écrire le fichier sur le disque : l'appelant peut-il envoyer dans cette
    collection ? (La RLS le vérifie de toute façon à l'insertion.)"""
    cur = await conn.execute(
        "SELECT id = ANY ((SELECT rag_collections('write'))::bigint[])"
        " FROM collections WHERE name = %s",
        (collection,),
    )
    row = await cur.fetchone()
    if row is None:  # inexistante, ou invisible pour l'appelant : même réponse
        raise UnknownCollection(collection)
    if not row[0]:
        raise CollectionForbidden(collection)


async def create_document(
    conn: psycopg.AsyncConnection,
    *,
    filename: str,
    sha256: str,
    size_bytes: int,
    created_by: int | None = None,
    collection: str = DEFAULT_COLLECTION,
) -> tuple[Document, bool]:
    """Enregistre le document et met un job d'ingestion dans la file.

    Renvoie (document, True) si un job a été créé. Un PDF déjà présent dans la collection
    n'est pas réindexé : (document existant, False) — sauf si son ingestion avait échoué,
    auquel cas il est remis dans la file.
    """
    async with conn.transaction():
        cur = await conn.execute("SELECT id FROM collections WHERE name = %s", (collection,))
        row = await cur.fetchone()
        if row is None:
            raise UnknownCollection(collection)
        collection_id = row[0]

        cur = await conn.execute(
            """
            INSERT INTO documents (collection_id, filename, sha256, size_bytes, created_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (collection_id, sha256) DO NOTHING
            RETURNING id
            """,
            (collection_id, filename, sha256, size_bytes, created_by),
        )
        row = await cur.fetchone()
        if row is not None:
            document_id, queued = row[0], True
        else:
            # Déjà présent : on le reprend seulement si l'ingestion précédente a échoué
            cur = await conn.execute(
                """
                UPDATE documents SET status = 'pending', error = NULL, updated_at = now()
                WHERE collection_id = %s AND sha256 = %s AND status = 'failed'
                RETURNING id
                """,
                (collection_id, sha256),
            )
            row = await cur.fetchone()
            if row is not None:
                document_id, queued = row[0], True
            else:
                cur = await conn.execute(
                    "SELECT id FROM documents WHERE collection_id = %s AND sha256 = %s",
                    (collection_id, sha256),
                )
                document_id, queued = (await cur.fetchone())[0], False  # type: ignore[index]
        if queued:
            await conn.execute("INSERT INTO jobs (document_id) VALUES (%s)", (document_id,))

    document = await get_document(conn, document_id)
    assert document is not None
    return document, queued
