"""Accès aux modèles par l'interface compatible OpenAI.

Aujourd'hui c'est Ollama ; un autre serveur (vLLM) ou un fournisseur hébergé
se branche en changeant LLM_BASE_URL, sans toucher au code.
"""

from dataclasses import dataclass
from functools import lru_cache

from openai import AsyncOpenAI, OpenAIError

from app.config import Settings, get_settings


class LLMError(Exception):
    """Le serveur de modèles est injoignable ou a renvoyé une erreur."""


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
        self, prompt: str, *, model: str, max_tokens: int, temperature: float
    ) -> Generation:
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
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
