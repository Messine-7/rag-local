"""Envoi de PDF à indexer et suivi de leur ingestion (phase 4).

L'API ne fait que ranger le fichier et créer un job : le worker (app/worker.py) fait le
travail lourd (lecture, découpage, embeddings), sans ralentir les questions.
"""

import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.documents import Document, create_document, get_document, list_documents

router = APIRouter(prefix="/documents", tags=["documents"])

Config = Annotated[Settings, Depends(get_settings)]
READ_SIZE = 1024 * 1024


def get_pool(request: Request) -> AsyncConnectionPool:
    """Pool de connexions ouvert au démarrage de l'API (app/main.py)."""
    return request.app.state.pool


Pool = Annotated[AsyncConnectionPool, Depends(get_pool)]


class DocumentOut(BaseModel):
    id: int
    collection: str
    filename: str
    sha256: str
    size_bytes: int
    pages: int | None
    status: str  # pending → processing → ready, ou failed (voir error)
    error: str | None
    chunks: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, document: Document) -> "DocumentOut":
        return cls(**document.__dict__)


@router.post(
    "",
    status_code=202,
    responses={200: {"description": "PDF déjà indexé (ou en cours) : rien n'est refait"}},
)
async def upload_document(
    file: UploadFile, pool: Pool, settings: Config, response: Response
) -> DocumentOut:
    """Envoie un PDF à indexer. Répond 202 dès que le PDF est dans la file d'ingestion ;
    suivre son état avec GET /documents/{id}."""
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    tmp = upload_dir / f".upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(READ_SIZE):
                if size == 0 and not chunk.startswith(b"%PDF-"):
                    raise HTTPException(status_code=415, detail="le fichier n'est pas un PDF")
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF trop gros (plus de {settings.max_upload_mb} Mo)",
                    )
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="fichier vide")
        sha256 = digest.hexdigest()
        # Rangé sous son empreinte : le même contenu n'est stocké qu'une fois
        tmp.replace(upload_dir / f"{sha256}.pdf")
    finally:
        tmp.unlink(missing_ok=True)

    filename = Path(file.filename or "document.pdf").name[:255]
    async with pool.connection() as conn:
        document, queued = await create_document(
            conn, filename=filename, sha256=sha256, size_bytes=size
        )
    if not queued:
        response.status_code = 200
    return DocumentOut.of(document)


@router.get("")
async def list_all(pool: Pool) -> list[DocumentOut]:
    """Tous les documents, du plus récent au plus ancien."""
    async with pool.connection() as conn:
        return [DocumentOut.of(d) for d in await list_documents(conn)]


@router.get("/{document_id}")
async def get_one(document_id: int, pool: Pool) -> DocumentOut:
    """État d'un document : statut d'ingestion, nombre de pages et de chunks, erreur."""
    async with pool.connection() as conn:
        document = await get_document(conn, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document inconnu")
    return DocumentOut.of(document)
