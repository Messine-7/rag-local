"""Réglages communs aux tests.

Tests avec base de données (fixtures db_url, sql, anon_client, client) : ignorés sans
TEST_DATABASE_URL (la CI en fournit une). En local : base rag_test du Postgres de dev
(compose.override.yml), avec dans .env ou dans l'environnement :
  TEST_DATABASE_URL=postgresql://rag:<POSTGRES_PASSWORD>@127.0.0.1:5433/rag_test
(127.0.0.1 et non localhost : sous Windows, localhost part en IPv6 et bloque sur le port de WSL.)
La base est vidée par les tests : son nom doit finir par _test, sinon ils refusent de tourner.
"""

import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

# Poste de dev Windows : psycopg en mode async ne fonctionne pas avec la boucle par défaut
# (Proactor). Sans effet sous Linux (CI, image Docker).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.auth import create_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

TEST_USER = "alice"
TEST_PASSWORD = "mot-de-passe-alice"


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


def run_async(make: Callable[[psycopg.AsyncConnection], Coroutine[Any, Any, Any]]) -> Any:
    """Exécute make(conn) sur une connexion async à la base de test (fonctions de app.*)."""

    async def main() -> Any:
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            return await make(conn)

    return asyncio.run(main())


@pytest.fixture
def db_url() -> str:
    if not DB_URL:
        pytest.skip("TEST_DATABASE_URL absent")
    return DB_URL


@pytest.fixture
def sql(db_url) -> Callable[..., list[tuple]]:
    """Requête directe sur la base de test : sql("SELECT …", (param,))."""

    def query(statement: str, params: tuple = ()) -> list[tuple]:
        with psycopg.connect(db_url, autocommit=True) as conn:
            cur = conn.execute(statement, params)
            return cur.fetchall() if cur.description else []

    return query


@pytest.fixture
def run_db(db_url) -> Callable[..., Any]:
    """run_db(lambda conn: fonction_async(conn, …)) : appelle le code async de app.*."""
    return run_async


@pytest.fixture
def anon_client(db_url, tmp_path, monkeypatch) -> Iterator[TestClient]:
    """API démarrée complètement (migrations comprises) sur une base vidée, sans jeton."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("CHUNK_SIZE", "200")
    monkeypatch.setenv("CHUNK_OVERLAP", "40")
    get_settings.cache_clear()
    with TestClient(app) as c:  # « with » : lance le démarrage de l'API (lifespan)
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute("TRUNCATE users, tokens, documents, chunks, jobs RESTART IDENTITY CASCADE")
        yield c
    get_settings.cache_clear()


@pytest.fixture
def client(anon_client) -> TestClient:
    """Même API, connectée en tant qu'alice (utilisateur sans droits d'administration)."""
    run_async(lambda conn: create_user(conn, TEST_USER, TEST_PASSWORD))
    response = anon_client.post(
        "/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    anon_client.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return anon_client
