import os

from fastapi import FastAPI

app = FastAPI(title="RAG local")


@app.get("/health")
async def health() -> dict[str, str]:
    """Indique que l'API répond et quelle version (commit) est déployée."""
    return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
