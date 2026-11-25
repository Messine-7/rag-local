import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI
from psycopg_pool import AsyncConnectionPool

from app.auth import admin_user, current_user
from app.config import get_settings
from app.db import check_api_connection, migrate, set_api_password
from app.routes_admin import router as admin_router
from app.routes_ask import router as ask_router
from app.routes_auth import router as auth_router
from app.routes_documents import collections_router
from app.routes_documents import router as documents_router
from app.routes_llm import router as llm_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Au démarrage : migrations (en propriétaire), puis pool de connexions sous le rôle
    rag_api, soumis à la Row-Level Security. Sans base, l'API ne démarre pas."""
    settings = get_settings()
    for name, value in (
        ("DATABASE_URL", settings.database_url),
        ("API_DATABASE_URL", settings.api_database_url),
        ("API_DB_PASSWORD", settings.api_db_password),
    ):
        if not value:
            raise RuntimeError(f"{name} manquant")
    async with await psycopg.AsyncConnection.connect(settings.database_url) as owner:
        await migrate(owner)
        await set_api_password(owner, settings.api_db_password)
        await owner.commit()

    pool = AsyncConnectionPool(settings.api_database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True, timeout=30)
    try:
        async with pool.connection() as conn:
            await check_api_connection(conn)
        app.state.pool = pool
        yield
    finally:
        await pool.close()


app = FastAPI(title="RAG local", lifespan=lifespan)
app.include_router(auth_router)  # /auth/login est public, les autres routes exigent un jeton
# Tout le reste exige un utilisateur connecté ; seul /health reste public (CI, supervision)
app.include_router(llm_router, dependencies=[Depends(current_user)])
app.include_router(documents_router, dependencies=[Depends(current_user)])
app.include_router(collections_router, dependencies=[Depends(current_user)])
app.include_router(ask_router, dependencies=[Depends(current_user)])
app.include_router(admin_router, dependencies=[Depends(admin_user)])


@app.get("/health")
async def health() -> dict[str, str]:
    """Indique que l'API répond et quelle version (commit) est déployée."""
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
