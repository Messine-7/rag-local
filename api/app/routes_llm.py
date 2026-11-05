"""Endpoints de la phase 3 : accès direct aux modèles, pour les tester et les mesurer.

Ils seront remplacés par POST /ask en phase 5.
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.llm import LLMClient, LLMError, get_llm

router = APIRouter(prefix="/llm", tags=["modèles"])

LLM = Annotated[LLMClient, Depends(get_llm)]
Config = Annotated[Settings, Depends(get_settings)]


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(
        default=None, description="Un des modèles autorisés ; par défaut LLM_MODEL"
    )
    max_tokens: int = Field(default=256, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class GenerateResponse(BaseModel):
    answer: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=64)


class EmbedResponse(BaseModel):
    model: str
    dimensions: int
    embeddings: list[list[float]]


def unavailable(exc: LLMError) -> HTTPException:
    return HTTPException(status_code=503, detail=f"serveur de modèles indisponible : {exc}")


@router.post("/generate")
async def generate(req: GenerateRequest, llm: LLM, settings: Config) -> GenerateResponse:
    """Génère une réponse à un prompt, et mesure le temps passé côté serveur."""
    model = req.model or settings.llm_model
    if model not in settings.llm_allowed_models:
        raise HTTPException(status_code=400, detail=f"modèle non autorisé : {model}")
    start = time.perf_counter()
    try:
        result = await llm.generate(
            req.prompt, model=model, max_tokens=req.max_tokens, temperature=req.temperature
        )
    except LLMError as exc:
        raise unavailable(exc) from exc
    return GenerateResponse(
        answer=result.text,
        model=model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=round((time.perf_counter() - start) * 1000),
    )


@router.post("/embed")
async def embed(req: EmbedRequest, llm: LLM, settings: Config) -> EmbedResponse:
    """Calcule les embeddings d'une liste de textes."""
    try:
        vectors = await llm.embed(req.texts, model=settings.embed_model)
    except LLMError as exc:
        raise unavailable(exc) from exc
    return EmbedResponse(
        model=settings.embed_model,
        dimensions=len(vectors[0]) if vectors else 0,
        embeddings=vectors,
    )
