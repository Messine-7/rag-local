"""Envoi de PDF et worker d'ingestion, contre un vrai Postgres + pgvector.

Ignorés sans TEST_DATABASE_URL (la CI en fournit une). En local : base rag_test du Postgres
de dev (compose.override.yml), avec dans .env ou dans l'environnement :
  TEST_DATABASE_URL=postgresql://rag:<POSTGRES_PASSWORD>@127.0.0.1:5433/rag_test
(127.0.0.1 et non localhost : sous Windows, localhost part en IPv6 et bloque sur le port de WSL.)
La base est vidée par les tests : son nom doit finir par _test, sinon ils refusent de tourner.
"""

import asyncio
import os
from pathlib import Path

import psycopg
import pymupdf
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from app.config import get_settings
from app.db import migrate
from app.llm import LLMError
from app.main import app
from app.worker import run_once


def _test_database_url() -> str:
    """TEST_DATABASE_URL de l'environnement, sinon du .env à la racine du repo."""
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "TEST_DATABASE_URL":
                return value.strip().strip("\"'")
    return ""


DB_URL = _test_database_url()
_DB_NAME = str(conninfo_to_dict(DB_URL).get("dbname") or "") if DB_URL else ""
if DB_URL and not _DB_NAME.endswith("_test"):
    # Garde-fou : les tests vident la base, jamais celle de dev ou du PC fixe
    raise pytest.UsageError(f"TEST_DATABASE_URL doit viser une base « …_test », pas « {_DB_NAME} »")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL absent")

DIMENSIONS = 1024


class FakeEmbedder:
    """Remplace bge-m3 : la CI n'a ni GPU ni Ollama."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def embed(self, texts, *, model):
        self.calls += 1
        if self.fail:
            raise LLMError("Ollama injoignable")
        return [[0.0] * (DIMENSIONS - 1) + [1.0] for _ in texts]


def make_pdf(*pages: str) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), text, fontsize=10)
    return doc.tobytes()


def query(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, params).fetchall()


def run_worker(embedder: FakeEmbedder) -> bool:
    async def once() -> bool:
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            return await run_once(conn, embedder, get_settings())

    return asyncio.run(once())


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("CHUNK_SIZE", "200")
    monkeypatch.setenv("CHUNK_OVERLAP", "40")
    get_settings.cache_clear()
    with TestClient(app) as c:  # « with » : démarrage complet, migrations comprises
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute("TRUNCATE documents, chunks, jobs RESTART IDENTITY CASCADE")
        yield c
    get_settings.cache_clear()


def upload(client: TestClient, content: bytes, name: str = "rapport.pdf"):
    return client.post("/documents", files={"file": (name, content, "application/pdf")})


def test_upload_queues_a_job(client, tmp_path):
    response = upload(client, make_pdf("Bonjour"))
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["filename"] == "rapport.pdf"
    assert body["collection"] == "défaut"
    assert (tmp_path / f"{body['sha256']}.pdf").exists()
    assert query("SELECT status FROM jobs WHERE document_id = %s", (body["id"],)) == [("pending",)]


def test_same_pdf_is_not_queued_twice(client):
    pdf = make_pdf("Bonjour")
    first = upload(client, pdf).json()
    second = upload(client, pdf, name="copie.pdf")
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert query("SELECT count(*) FROM jobs") == [(1,)]


def test_rejects_non_pdf(client):
    assert upload(client, b"pas un pdf", name="x.pdf").status_code == 415


def test_get_and_list(client):
    doc_id = upload(client, make_pdf("Bonjour")).json()["id"]
    assert client.get(f"/documents/{doc_id}").json()["id"] == doc_id
    assert [d["id"] for d in client.get("/documents").json()] == [doc_id]
    assert client.get("/documents/999999").status_code == 404


def test_worker_indexes_document(client):
    text = "Le RAG local indexe des PDF. " * 30
    doc_id = upload(client, make_pdf(text, "", "Troisième page.")).json()["id"]

    assert run_worker(FakeEmbedder()) is True
    assert run_worker(FakeEmbedder()) is False  # file vide

    body = client.get(f"/documents/{doc_id}").json()
    assert body["status"] == "ready"
    assert body["pages"] == 3
    assert body["chunks"] > 1
    rows = query("SELECT DISTINCT page, embed_model FROM chunks ORDER BY page")
    assert rows == [(1, "bge-m3"), (3, "bge-m3")]
    assert query("SELECT status FROM jobs") == [("done",)]
    # Recherche vectorielle possible : le vecteur stocké est bien un vector(1024)
    query_vec = "[" + ",".join(["0"] * (DIMENSIONS - 1) + ["1"]) + "]"
    nearest = query(
        "SELECT page FROM chunks ORDER BY embedding <=> %s::vector LIMIT 1", (query_vec,)
    )
    assert len(nearest) == 1


def test_pdf_without_text_fails_without_retry(client):
    doc_id = upload(client, make_pdf("")).json()["id"]
    run_worker(FakeEmbedder())
    body = client.get(f"/documents/{doc_id}").json()
    assert body["status"] == "failed"
    assert "aucun texte" in body["error"]
    assert query("SELECT status, attempts FROM jobs") == [("failed", 1)]


def test_embedding_error_is_retried_later(client):
    doc_id = upload(client, make_pdf("Bonjour")).json()["id"]
    run_worker(FakeEmbedder(fail=True))
    assert client.get(f"/documents/{doc_id}").json()["status"] == "pending"
    status, attempts, delayed = query("SELECT status, attempts, run_after > now() FROM jobs")[0]
    assert (status, attempts, delayed) == ("pending", 1, True)
    assert run_worker(FakeEmbedder()) is False  # pas avant le délai


def test_failed_document_can_be_sent_again(client):
    pdf = make_pdf("")
    doc_id = upload(client, pdf).json()["id"]
    run_worker(FakeEmbedder())
    again = upload(client, pdf)
    assert again.status_code == 202
    assert again.json()["id"] == doc_id
    assert again.json()["status"] == "pending"


def test_migrations_are_applied_once(client):
    async def migrate_again() -> list[str]:
        async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
            return await migrate(conn)

    assert asyncio.run(migrate_again()) == []
