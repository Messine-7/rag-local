import os

from fastapi import FastAPI

from app.routes_llm import router as llm_router

app = FastAPI(title="RAG local")
app.include_router(llm_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Indique que l'API répond et quelle version (commit) est déployée."""
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
