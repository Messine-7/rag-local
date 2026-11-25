"""Questions sur les documents, et historique des conversations (phase 5.3).

POST /ask, dans l'ordre :
1. conversation reprise (conversation_id) ou créée ; question enregistrée ;
2. embedding de la question (bge-m3), puis recherche hybride parmi les chunks que
   l'appelant peut lire (Row-Level Security) ;
3. prompt : consignes, extraits numérotés, historique récent, question ;
4. réponse du modèle au fil de l'eau (Server-Sent Events), puis enregistrée avec ses
   citations, ses tokens et sa durée.

Évènements SSE (stream=true, par défaut) :
  meta  {"conversation_id", "model", "citations": [...]}   avant le premier mot
  token {"text"}                                            morceaux de la réponse
  done  {"message_id", "prompt_tokens", "completion_tokens", "latency_ms"}
  error {"detail"}                                          le modèle a échoué en route
stream=false : une seule réponse JSON, quand tout est fini (scripts, tests).
"""

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.auth import CallerDB, CurrentCaller, caller_connection
from app.config import Settings, get_settings
from app.conversations import (
    NO_PASSAGE_ANSWER,
    UnknownConversation,
    add_message,
    build_messages,
    citations_of,
    create_conversation,
    delete_conversation,
    get_conversation,
    get_messages,
    list_conversations,
)
from app.llm import LLMClient, LLMError, Usage, get_llm
from app.retrieval import search

router = APIRouter(tags=["questions"])

LLM = Annotated[LLMClient, Depends(get_llm)]
Config = Annotated[Settings, Depends(get_settings)]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = Field(default=None, description="Absent : nouvelle conversation")
    model: str | None = Field(
        default=None, description="Un des modèles autorisés ; LLM_MODEL sinon"
    )
    collections: list[str] | None = Field(
        default=None, description="Limiter la recherche à ces collections (parmi les accessibles)"
    )
    stream: bool = Field(default=True, description="Réponse au fil de l'eau (SSE) ou en un bloc")


class Citation(BaseModel):
    n: int  # numéro cité dans la réponse : [n]
    chunk_id: int
    document_id: int
    filename: str
    collection: str
    page: int
    score: float
    excerpt: str


class AskResponse(BaseModel):
    conversation_id: int
    message_id: int
    answer: str
    model: str
    citations: list[Citation]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class ConversationOut(BaseModel):
    id: int
    title: str
    messages: int
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    model: str | None
    citations: list[Citation] | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    created_at: datetime


class ConversationDetail(ConversationOut):
    history: list[MessageOut]


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/ask",
    responses={
        200: {
            "description": "Flux text/event-stream (stream=true) ou AskResponse (stream=false)",
            "content": {"text/event-stream": {}},
        },
        404: {"description": "conversation inconnue"},
        503: {"description": "serveur de modèles indisponible"},
    },
)
async def ask(
    req: AskRequest,
    request: Request,
    conn: CallerDB,
    caller: CurrentCaller,
    llm: LLM,
    settings: Config,
) -> Any:
    """Pose une question sur les documents accessibles ; réponse avec citations [n]."""
    start = time.perf_counter()
    model = req.model or settings.llm_model
    if model not in settings.llm_allowed_models:
        raise HTTPException(status_code=400, detail=f"modèle non autorisé : {model}")

    # 1. Conversation et historique (avant d'y ajouter la question)
    if req.conversation_id is not None:
        try:
            conversation_id = (await get_conversation(conn, req.conversation_id)).id
        except UnknownConversation as exc:
            raise HTTPException(status_code=404, detail="conversation inconnue") from exc
        history = await get_messages(conn, conversation_id, last=settings.ask_history_messages)
    else:
        conversation_id = await create_conversation(conn, caller.user.id, req.question.strip())
        history = []

    # 2. Passages utiles, parmi ceux que l'appelant peut lire
    try:
        [vector] = await llm.embed([req.question], model=settings.embed_model)
    except LLMError as exc:
        raise HTTPException(
            status_code=503, detail=f"serveur de modèles indisponible : {exc}"
        ) from exc
    passages = await search(
        conn,
        question=req.question,
        question_vector=vector,
        top_k=settings.ask_top_k,
        candidates=settings.ask_candidates,
        collections=req.collections,
    )
    citations = citations_of(passages)
    await add_message(conn, conversation_id, "user", req.question)
    # La transaction de la requête est validée ici, avant la réponse (Depends scope="function") :
    # la réponse du modèle est enregistrée ensuite, sur une autre connexion.

    messages = build_messages(req.question, passages, history)
    pool = request.app.state.pool

    async def answer() -> AsyncIterator[str | Usage]:
        if not passages:  # rien d'accessible : pas d'appel au modèle, pas d'invention possible
            yield NO_PASSAGE_ANSWER
            yield Usage(0, 0)
            return
        async for part in llm.stream_chat(
            messages,
            model=model,
            max_tokens=settings.ask_max_tokens,
            temperature=settings.ask_temperature,
            reasoning_effort=settings.ask_reasoning_effort or None,
        ):
            yield part

    async def save_on(target: psycopg.AsyncConnection, text: str, usage: Usage) -> tuple[int, int]:
        latency_ms = round((time.perf_counter() - start) * 1000)
        message_id = await add_message(
            target,
            conversation_id,
            "assistant",
            text,
            model=model if passages else None,
            citations=citations,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
        )
        return message_id, latency_ms

    async def save(text: str, usage: Usage) -> tuple[int, int]:
        """Après la fin de la requête (streaming) : sur une nouvelle connexion, la transaction
        de la requête étant déjà validée."""
        async with caller_connection(pool, caller.token_hash) as save_conn:
            return await save_on(save_conn, text, usage)

    if not req.stream:
        parts: list[str] = []
        usage = Usage(0, 0)
        try:
            async for part in answer():
                if isinstance(part, Usage):
                    usage = part
                else:
                    parts.append(part)
        except LLMError as exc:
            raise HTTPException(
                status_code=503, detail=f"serveur de modèles indisponible : {exc}"
            ) from exc
        text = "".join(parts)
        # Encore dans la requête : même connexion et même transaction que la question
        message_id, latency_ms = await save_on(conn, text, usage)
        return AskResponse(
            conversation_id=conversation_id,
            message_id=message_id,
            answer=text,
            model=model,
            citations=[Citation(**c) for c in citations],
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
        )

    async def events() -> AsyncIterator[str]:
        yield _sse(
            "meta", {"conversation_id": conversation_id, "model": model, "citations": citations}
        )
        parts: list[str] = []
        usage = Usage(0, 0)
        try:
            async for part in answer():
                if isinstance(part, Usage):
                    usage = part
                else:
                    parts.append(part)
                    yield _sse("token", {"text": part})
        except LLMError as exc:
            yield _sse("error", {"detail": f"serveur de modèles indisponible : {exc}"})
            return
        message_id, latency_ms = await save("".join(parts), usage)
        yield _sse(
            "done",
            {
                "message_id": message_id,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "latency_ms": latency_ms,
            },
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Ni cache ni mise en mémoire tampon par un proxy : chaque mot part tout de suite
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations")
async def my_conversations(conn: CallerDB) -> list[ConversationOut]:
    """Ses conversations, de la plus récente à la plus ancienne."""
    return [ConversationOut(**c.__dict__) for c in await list_conversations(conn)]


@router.get("/conversations/{conversation_id}")
async def conversation_detail(
    conversation_id: Annotated[int, Path(ge=1)], conn: CallerDB
) -> ConversationDetail:
    """Une de ses conversations, avec tous ses messages (pour la reprendre)."""
    try:
        conversation = await get_conversation(conn, conversation_id)
    except UnknownConversation as exc:
        raise HTTPException(status_code=404, detail="conversation inconnue") from exc
    history = await get_messages(conn, conversation_id)
    return ConversationDetail(
        **conversation.__dict__, history=[MessageOut(**m.__dict__) for m in history]
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def remove_conversation(conversation_id: Annotated[int, Path(ge=1)], conn: CallerDB) -> None:
    if not await delete_conversation(conn, conversation_id):
        raise HTTPException(status_code=404, detail="conversation inconnue")
