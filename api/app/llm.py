"""Accès aux modèles par l'interface compatible OpenAI.

Aujourd'hui c'est Ollama ; un autre serveur (vLLM) ou un fournisseur hébergé
se branche en changeant LLM_BASE_URL, sans toucher au code.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache

from openai import AsyncOpenAI, OpenAIError

from app.config import Settings, get_settings


class LLMError(Exception):
    """Le serveur de modèles est injoignable ou a renvoyé une erreur."""


@dataclass(frozen=True)
class Usage:
    """Tokens consommés, envoyés en dernier par stream_chat."""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        # Pas de nouvel essai automatique : une erreur doit se voir, surtout pendant les mesures
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
            max_retries=0,
        )

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None = None,
    ) -> Generation:
        """reasoning_effort : voir stream_chat."""
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
                **extra,  # type: ignore[arg-type]
            )
        except OpenAIError as exc:
            raise LLMError(str(exc)) from exc
        if not response.choices:
            raise LLMError("réponse vide du serveur de modèles")
        usage = response.usage
        return Generation(
            text=response.choices[0].message.content or "",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[str | Usage]:
        """Réponse au fil de l'eau : des morceaux de texte, puis un Usage à la fin.

        reasoning_effort="none" : pas de raisonnement préalable pour les modèles qui en font
        (gemma4). Sans cela, il consomme les max_tokens en réflexion invisible et la réponse
        est coupée, voire vide. Ignoré par les autres modèles (mistral)."""
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
                **extra,  # type: ignore[arg-type]
            )
            usage = Usage(0, 0)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                if chunk.usage:
                    usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        except OpenAIError as exc:
            raise LLMError(str(exc)) from exc
        yield usage

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(
                model=model, input=texts, encoding_format="float"
            )
        except OpenAIError as exc:
            raise LLMError(str(exc)) from exc
        return [item.embedding for item in response.data]


@lru_cache
def get_llm() -> LLMClient:
    """Un seul client partagé par toute l'API (il gère son propre pool de connexions)."""
    return LLMClient(get_settings())
