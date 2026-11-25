"""Recherche des passages utiles à une question (phase 5.3) : recherche hybride.

Deux classements des chunks, fusionnés par Reciprocal Rank Fusion (RRF) :
- sens : distance cosinus entre l'embedding de la question et celui du chunk (index HNSW) ;
- mots : recherche plein texte en français (tsvector, index GIN), utile pour les noms
  propres, sigles et numéros que l'embedding rend mal.
Score RRF d'un chunk = somme sur les deux classements de 1 / (60 + rang).

La connexion est celle de l'appelant (CallerDB) : la Row-Level Security ne laisse voir que
les chunks des collections qu'il peut lire. Avec un filtre, l'index HNSW pourrait renvoyer
moins de résultats que demandé (il filtre après coup) : hnsw.iterative_scan (pgvector 0.8)
lui fait poursuivre la recherche jusqu'à en avoir assez.
"""

from dataclasses import dataclass

import psycopg
from psycopg.rows import class_row

from app.db import to_vector

RRF_K = 60  # constante habituelle de la RRF : atténue l'écart entre les premiers rangs


@dataclass(frozen=True)
class Passage:
    chunk_id: int
    document_id: int
    filename: str
    collection: str
    page: int
    content: str
    score: float


_SEARCH = """
WITH vec AS (
    SELECT id, row_number() OVER (ORDER BY dist) AS rank
    FROM (
        SELECT id, embedding <=> %(qv)s::vector AS dist
        FROM chunks
        WHERE %(collections)s::text[] IS NULL
           OR collection_id IN (
               SELECT id FROM collections WHERE name = ANY (%(collections)s::text[]))
        ORDER BY embedding <=> %(qv)s::vector
        LIMIT %(candidates)s
    ) v
),
txt AS (
    SELECT id, row_number() OVER (ORDER BY rank_cd DESC) AS rank
    FROM (
        SELECT c.id, ts_rank_cd(c.content_tsv, q) AS rank_cd
        FROM chunks c, websearch_to_tsquery('french', %(q)s) q
        WHERE c.content_tsv @@ q
          AND (%(collections)s::text[] IS NULL
               OR c.collection_id IN (
                   SELECT id FROM collections WHERE name = ANY (%(collections)s::text[])))
        ORDER BY rank_cd DESC
        LIMIT %(candidates)s
    ) t
),
fused AS (
    SELECT id, sum(1.0 / (%(rrf_k)s + rank)) AS score
    FROM (SELECT id, rank FROM vec UNION ALL SELECT id, rank FROM txt) u
    GROUP BY id
)
SELECT c.id AS chunk_id, c.document_id, d.filename, col.name AS collection, c.page,
       c.content, f.score::float AS score
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
JOIN collections col ON col.id = c.collection_id
ORDER BY f.score DESC, c.id
LIMIT %(top_k)s
"""


async def search(
    conn: psycopg.AsyncConnection,
    *,
    question: str,
    question_vector: list[float],
    top_k: int,
    candidates: int,
    collections: list[str] | None = None,
) -> list[Passage]:
    """Les top_k meilleurs passages accessibles à l'appelant, du plus pertinent au moins.

    candidates : nombre de chunks retenus par chaque classement avant la fusion.
    collections : limite la recherche à ces collections (parmi celles accessibles)."""
    # SET LOCAL : seulement pour cette transaction (celle de la requête)
    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    await conn.execute(f"SET LOCAL hnsw.ef_search = {max(40, int(candidates))}")
    async with conn.cursor(row_factory=class_row(Passage)) as cur:
        await cur.execute(
            _SEARCH,
            {
                "qv": to_vector(question_vector),
                "q": question,
                "collections": collections or None,
                "candidates": candidates,
                "rrf_k": RRF_K,
                "top_k": top_k,
            },
        )
        return await cur.fetchall()
