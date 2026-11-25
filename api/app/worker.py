"""Worker d'ingestion : lit la file de jobs dans Postgres et indexe les PDF.

Lancé dans son propre conteneur, avec la même image que l'API :  python -m app.worker

Pour chaque job : lecture du PDF, découpage en chunks, embeddings (bge-m3 via Ollama),
puis insertion des chunks en une transaction. Plusieurs workers peuvent tourner ensemble :
SELECT … FOR UPDATE SKIP LOCKED garantit qu'un job n'est pris que par un seul.
"""

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psycopg

from app.chunking import chunk_pages
from app.config import Settings, get_settings
from app.db import migrate, to_vector
from app.llm import LLMClient
from app.pdf import PdfError, extract_pages

log = logging.getLogger("worker")


class Embedder(Protocol):
    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...


class PermanentError(Exception):
    """Échec qui se reproduirait à l'identique : pas de nouvel essai."""


@dataclass(frozen=True)
class Job:
    id: int
    document_id: int
    attempts: int  # essais, celui-ci compris


async def claim_job(conn: psycopg.AsyncConnection) -> Job | None:
    """Prend le plus ancien job prêt, sans jamais prendre celui d'un autre worker."""
    cur = await conn.execute(
        """
        UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'pending' AND run_after <= now()
            ORDER BY run_after, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, document_id, attempts
        """
    )
    row = await cur.fetchone()
    return Job(*row) if row else None


async def requeue_stale_jobs(conn: psycopg.AsyncConnection, timeout_s: int) -> int:
    """Remet dans la file les jobs « running » depuis trop longtemps (worker arrêté net)."""
    cur = await conn.execute(
        """
        UPDATE jobs SET status = 'pending', run_after = now()
        WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)
        """,
        (timeout_s,),
    )
    if cur.rowcount:
        log.warning("%d job(s) abandonné(s) remis dans la file", cur.rowcount)
    return cur.rowcount


async def ingest(
    conn: psycopg.AsyncConnection, job: Job, embedder: Embedder, settings: Settings
) -> int:
    """Indexe le document du job ; renvoie le nombre de chunks."""
    cur = await conn.execute(
        "SELECT sha256, collection_id FROM documents WHERE id = %s", (job.document_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise PermanentError("document supprimé")
    sha256, collection_id = row
    path = Path(settings.upload_dir) / f"{sha256}.pdf"
    if not path.exists():
        raise PermanentError(f"fichier introuvable : {path.name}")

    try:
        pages = await asyncio.to_thread(extract_pages, path)  # CPU : hors de la boucle async
    except PdfError as exc:
        raise PermanentError(str(exc)) from exc
    chunks = chunk_pages(pages, settings.chunk_size, settings.chunk_overlap)
    if not chunks:
        raise PermanentError("aucun texte extrait (PDF scanné ? l'OCR n'est pas géré)")

    vectors: list[list[float]] = []
    for start in range(0, len(chunks), settings.embed_batch_size):
        batch = chunks[start : start + settings.embed_batch_size]
        vectors.extend(await embedder.embed([c.content for c in batch], model=settings.embed_model))
    if len(vectors) != len(chunks):
        raise RuntimeError(f"{len(vectors)} embeddings reçus pour {len(chunks)} chunks")
    if any(len(v) != settings.embed_dimensions for v in vectors):
        raise PermanentError(
            f"le modèle {settings.embed_model} ne renvoie pas des vecteurs de "
            f"{settings.embed_dimensions} dimensions (colonne chunks.embedding)"
        )

    # Tout ou rien : un document n'est jamais à moitié indexé
    async with conn.transaction():
        await conn.execute("DELETE FROM chunks WHERE document_id = %s", (job.document_id,))
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO chunks
                    (document_id, collection_id, chunk_index, page, content, embedding, embed_model)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                """,
                [
                    (
                        job.document_id,
                        collection_id,
                        i,
                        c.page,
                        c.content,
                        to_vector(v),
                        settings.embed_model,
                    )
                    for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
                ],
            )
        await conn.execute(
            "UPDATE documents SET status = 'ready', pages = %s, error = NULL, updated_at = now()"
            " WHERE id = %s",
            (len(pages), job.document_id),
        )
        await conn.execute(
            "UPDATE jobs SET status = 'done', error = NULL, finished_at = now() WHERE id = %s",
            (job.id,),
        )
    return len(chunks)


async def record_failure(
    conn: psycopg.AsyncConnection, job: Job, error: str, *, retry: bool
) -> None:
    async with conn.transaction():
        if retry:
            # Nouvel essai plus tard : 1 min, puis 2 min… (Ollama redémarre, GPU occupé…)
            await conn.execute(
                "UPDATE jobs SET status = 'pending', error = %s,"
                " run_after = now() + make_interval(mins => %s) WHERE id = %s",
                (error, job.attempts, job.id),
            )
            doc_status = "pending"
        else:
            await conn.execute(
                "UPDATE jobs SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (error, job.id),
            )
            doc_status = "failed"
        await conn.execute(
            "UPDATE documents SET status = %s, error = %s, updated_at = now() WHERE id = %s",
            (doc_status, error, job.document_id),
        )


async def run_once(conn: psycopg.AsyncConnection, embedder: Embedder, settings: Settings) -> bool:
    """Traite un job s'il y en a un ; renvoie False si la file est vide.

    La connexion doit être en autocommit : la prise du job est validée tout de suite.
    """
    job = await claim_job(conn)
    if job is None:
        return False
    await conn.execute(
        "UPDATE documents SET status = 'processing', updated_at = now() WHERE id = %s",
        (job.document_id,),
    )
    log.info("job %d : document %d, essai %d", job.id, job.document_id, job.attempts)
    started = time.perf_counter()
    try:
        count = await ingest(conn, job, embedder, settings)
    except Exception as exc:
        retry = not isinstance(exc, PermanentError) and job.attempts < settings.job_max_attempts
        log.error("job %d en échec (%s) : %s", job.id, "nouvel essai" if retry else "abandon", exc)
        await record_failure(conn, job, str(exc)[:2000] or type(exc).__name__, retry=retry)
    else:
        log.info("job %d : %d chunks en %.1f s", job.id, count, time.perf_counter() - started)
    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s"
    )
    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL manquant")
    embedder = LLMClient(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)  # finit le job en cours, puis s'arrête

    async with await psycopg.AsyncConnection.connect(
        settings.database_url, autocommit=True
    ) as conn:
        await migrate(conn)
        log.info("worker prêt (modèle d'embeddings : %s)", settings.embed_model)
        last_stale_check = 0.0
        while not stop.is_set():
            if time.monotonic() - last_stale_check > 60:
                await requeue_stale_jobs(conn, settings.job_timeout_s)
                last_stale_check = time.monotonic()
            if await run_once(conn, embedder, settings):
                continue  # file non vide : job suivant sans attendre
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_s)
            except TimeoutError:
                pass
    log.info("worker arrêté")


if __name__ == "__main__":
    asyncio.run(main())
