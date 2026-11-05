"""Configuration de l'API, lue dans les variables d'environnement.

Changer de modèle ou de serveur de modèles ne demande que de changer ces variables.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_allowed_models: tuple[str, ...]
    embed_model: str
    llm_timeout_s: float


def load_settings(env: Mapping[str, str]) -> Settings:
    model = env.get("LLM_MODEL", "mistral-small3.2:24b")
    allowed = [
        name.strip()
        for name in env.get("LLM_ALLOWED_MODELS", "mistral-small3.2:24b,gemma4:12b").split(",")
        if name.strip()
    ]
    if model not in allowed:
        allowed.insert(0, model)  # le modèle par défaut est toujours autorisé
    return Settings(
        llm_base_url=env.get("LLM_BASE_URL", "http://ollama:11434/v1"),
        llm_api_key=env.get("LLM_API_KEY", "ollama"),  # ignorée par Ollama, exigée par le client
        llm_model=model,
        llm_allowed_models=tuple(allowed),
        embed_model=env.get("EMBED_MODEL", "bge-m3"),
        llm_timeout_s=float(env.get("LLM_TIMEOUT_S", "300")),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings(os.environ)
