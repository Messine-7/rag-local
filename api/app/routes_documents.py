"""Envoi de PDF à indexer, suivi de leur ingestion (phase 4), et collections (phase 5.2).

L'API ne fait que ranger le fichier et créer un job : le worker (app/worker.py) fait le
travail lourd (lecture, découpage, embeddings), sans ralentir les questions.

Chaque route travaille sur une connexion soumise aux droits de l'appelant (CallerDB) :
Postgres ne montre que les documents des collections qu'il peut lire.
"""

import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app.auth import CallerDB, CurrentUser
from app.config import Settings, get_settings
from app.documents import (
    DEFAULT_COLLECTION,
    CollectionForbidden,
    Document,
    UnknownCollection,
    check_can_write,
    create_document,
    get_document,
    list_collections,
    list_documents,
)

router = APIRouter(prefix="/documents", tags=["documents"])
collections_router = APIRouter(prefix="/collections", tags=["documents"])

Config = Annotated[Settings, Depends(get_settings)]
READ_SIZE = 1024 * 1024


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
    uploaded_by: str | None  # utilisateur qui l'a envoyé
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, document: Document) -> "DocumentOut":
        return cls(**document.__dict__)


class CollectionOut(BaseModel):
    id: int
    name: str
    permission: str  # read : lire et interroger ; write : en plus, envoyer des PDF


@router.post(
    "",
    status_code=202,
    responses={
        200: {"description": "PDF déjà indexé (ou en cours) : rien n'est refait"},
        403: {"description": "droit de lecture seulement sur la collection"},
        404: {"description": "collection inconnue ou inaccessible"},
    },
)
async def upload_document(
    file: UploadFile,
    conn: CallerDB,
    settings: Config,
    response: Response,
    user: CurrentUser,
    collection: Annotated[str, Form()] = DEFAULT_COLLECTION,
) -> DocumentOut:
    """Envoie un PDF à indexer dans une collection (droit d'écriture requis). Répond 202 dès
    que le PDF est dans la file d'ingestion ; suivre son état avec GET /documents/{id}."""
    try:
        await check_can_write(conn, collection)  # avant d'écrire quoi que ce soit sur le disque
    except UnknownCollection as exc:
        raise HTTPException(status_code=404, detail=f"collection inconnue : {collection}") from exc
    except CollectionForbidden as exc:
        raise HTTPException(
            status_code=403, detail=f"pas de droit d'écriture sur la collection {collection}"
        ) from exc

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
    document, queued = await create_document(
        conn,
        filename=filename,
        sha256=sha256,
        size_bytes=size,
        created_by=user.id,
        collection=collection,
    )
    if not queued:
        response.status_code = 200
    return DocumentOut.of(document)


@router.get("")
async def list_all(conn: CallerDB) -> list[DocumentOut]:
    """Documents des collections accessibles, du plus récent au plus ancien."""
    return [DocumentOut.of(d) for d in await list_documents(conn)]


@router.get("/{document_id}")
async def get_one(document_id: int, conn: CallerDB) -> DocumentOut:
    """État d'un document : statut d'ingestion, nombre de pages et de chunks, erreur.
    404 aussi pour un document d'une collection inaccessible (son existence n'est pas révélée)."""
    document = await get_document(conn, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document inconnu")
    return DocumentOut.of(document)


@collections_router.get("")
async def my_collections(conn: CallerDB) -> list[CollectionOut]:
    """Collections accessibles à l'appelant, avec son droit sur chacune."""
    return [CollectionOut(**c.__dict__) for c in await list_collections(conn)]
