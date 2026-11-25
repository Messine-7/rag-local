"""Configuration de l'API et du worker, lue dans les variables d'environnement.

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
    # Phase 4 : base de données et ingestion
    database_url: str = ""
    upload_dir: str = "/data/uploads"  # volume partagé entre l'API et le worker
    max_upload_mb: int = 100
    chunk_size: int = 1500  # caractères (environ 350 à 400 tokens)
    chunk_overlap: int = 200  # caractères repris du chunk précédent
    embed_batch_size: int = 16  # textes envoyés ensemble au modèle d'embeddings
    # Taille des vecteurs : doit correspondre à la colonne chunks.embedding (bge-m3 : 1024).
    # Changer de modèle d'embeddings demande une migration et une réindexation complète.
    embed_dimensions: int = 1024
    worker_poll_s: float = 3.0  # attente entre deux regards dans la file quand elle est vide
    job_timeout_s: int = 900  # au-delà, un job « running » est considéré comme abandonné
    job_max_attempts: int = 3
    # Phase 5 : connexion
    session_ttl_hours: int = 168  # durée d'un jeton de session (7 jours)


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
        database_url=env.get("DATABASE_URL", ""),
        upload_dir=env.get("UPLOAD_DIR", "/data/uploads"),
        max_upload_mb=int(env.get("MAX_UPLOAD_MB", "100")),
        chunk_size=int(env.get("CHUNK_SIZE", "1500")),
        chunk_overlap=int(env.get("CHUNK_OVERLAP", "200")),
        embed_batch_size=int(env.get("EMBED_BATCH_SIZE", "16")),
        worker_poll_s=float(env.get("WORKER_POLL_S", "3")),
        job_timeout_s=int(env.get("JOB_TIMEOUT_S", "900")),
        job_max_attempts=int(env.get("JOB_MAX_ATTEMPTS", "3")),
        session_ttl_hours=int(env.get("SESSION_TTL_HOURS", "168")),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings(os.environ)
