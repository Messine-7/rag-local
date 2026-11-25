import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.db import migrate
from app.routes_documents import router as documents_router
from app.routes_llm import router as llm_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Au démarrage : connexion à Postgres et migrations ; sans base, l'API ne démarre pas."""
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL manquant")
    pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True, timeout=30)
    try:
        async with pool.connection() as conn:
            await migrate(conn)
        app.state.pool = pool
        yield
    finally:
        await pool.close()


app = FastAPI(title="RAG local", lifespan=lifespan)
app.include_router(llm_router)
app.include_router(documents_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Indique que l'API répond et quelle version (commit) est déployée."""
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
