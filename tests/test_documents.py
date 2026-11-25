"""Envoi de PDF et worker d'ingestion, contre un vrai Postgres + pgvector.

Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import migrate
from app.llm import LLMError
from app.worker import run_once

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


@pytest.fixture
def worker(run_db):
    """worker(embedder) : le worker traite un job ; True s'il en a trouvé un."""

    def run(embedder: FakeEmbedder) -> bool:
        return run_db(lambda conn: run_once(conn, embedder, get_settings()))

    return run


def upload(client: TestClient, content: bytes, name: str = "rapport.pdf"):
    return client.post("/documents", files={"file": (name, content, "application/pdf")})


def test_upload_queues_a_job(client, sql, tmp_path):
    response = upload(client, make_pdf("Bonjour"))
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["filename"] == "rapport.pdf"
    assert body["collection"] == "défaut"
    assert body["uploaded_by"] == "alice"
    assert (tmp_path / f"{body['sha256']}.pdf").exists()
    assert sql("SELECT status FROM jobs WHERE document_id = %s", (body["id"],)) == [("pending",)]


def test_documents_require_a_token(anon_client):
    assert anon_client.get("/documents").status_code == 401
    assert upload(anon_client, make_pdf("Bonjour")).status_code == 401


def test_same_pdf_is_not_queued_twice(client, sql):
    pdf = make_pdf("Bonjour")
    first = upload(client, pdf).json()
    second = upload(client, pdf, name="copie.pdf")
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert sql("SELECT count(*) FROM jobs") == [(1,)]


def test_rejects_non_pdf(client):
    assert upload(client, b"pas un pdf", name="x.pdf").status_code == 415


def test_get_and_list(client):
    doc_id = upload(client, make_pdf("Bonjour")).json()["id"]
    assert client.get(f"/documents/{doc_id}").json()["id"] == doc_id
    assert [d["id"] for d in client.get("/documents").json()] == [doc_id]
    assert client.get("/documents/999999").status_code == 404


def test_worker_indexes_document(client, sql, worker):
    text = "Le RAG local indexe des PDF. " * 30
    doc_id = upload(client, make_pdf(text, "", "Troisième page.")).json()["id"]

    assert worker(FakeEmbedder()) is True
    assert worker(FakeEmbedder()) is False  # file vide

    body = client.get(f"/documents/{doc_id}").json()
    assert body["status"] == "ready"
    assert body["pages"] == 3
    assert body["chunks"] > 1
    rows = sql("SELECT DISTINCT page, embed_model FROM chunks ORDER BY page")
    assert rows == [(1, "bge-m3"), (3, "bge-m3")]
    assert sql("SELECT status FROM jobs") == [("done",)]
    # Recherche vectorielle possible : le vecteur stocké est bien un vector(1024)
    query_vec = "[" + ",".join(["0"] * (DIMENSIONS - 1) + ["1"]) + "]"
    nearest = sql("SELECT page FROM chunks ORDER BY embedding <=> %s::vector LIMIT 1", (query_vec,))
    assert len(nearest) == 1


def test_pdf_without_text_fails_without_retry(client, sql, worker):
    doc_id = upload(client, make_pdf("")).json()["id"]
    worker(FakeEmbedder())
    body = client.get(f"/documents/{doc_id}").json()
    assert body["status"] == "failed"
    assert "aucun texte" in body["error"]
    assert sql("SELECT status, attempts FROM jobs") == [("failed", 1)]


def test_embedding_error_is_retried_later(client, sql, worker):
    doc_id = upload(client, make_pdf("Bonjour")).json()["id"]
    worker(FakeEmbedder(fail=True))
    assert client.get(f"/documents/{doc_id}").json()["status"] == "pending"
    status, attempts, delayed = sql("SELECT status, attempts, run_after > now() FROM jobs")[0]
    assert (status, attempts, delayed) == ("pending", 1, True)
    assert worker(FakeEmbedder()) is False  # pas avant le délai


def test_failed_document_can_be_sent_again(client, worker):
    pdf = make_pdf("")
    doc_id = upload(client, pdf).json()["id"]
    worker(FakeEmbedder())
    again = upload(client, pdf)
    assert again.status_code == 202
    assert again.json()["id"] == doc_id
    assert again.json()["status"] == "pending"


def test_migrations_are_applied_once(anon_client, run_db):
    assert run_db(migrate) == []
