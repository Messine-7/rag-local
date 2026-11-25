"""Rôle rag_readonly (migration 003) : lecture sans écriture, secrets exclus.

Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

import psycopg
import pytest


@pytest.fixture
def as_reader(anon_client, db_url):
    """Exécute une requête sous le rôle rag_readonly (SET ROLE, dans une transaction annulée)."""

    def run(statement: str):
        with psycopg.connect(db_url) as conn:
            conn.execute("SET ROLE rag_readonly")
            try:
                return conn.execute(statement).fetchall()
            finally:
                conn.rollback()

    return run


def test_reader_can_read_documents_and_users(as_reader):
    assert as_reader("SELECT count(*) FROM documents") == [(0,)]
    assert as_reader("SELECT count(*) FROM chunks") == [(0,)]
    assert as_reader("SELECT id, username, is_admin FROM users") == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT password_hash FROM users",
        "SELECT * FROM users",  # comprend password_hash
        "SELECT token_hash FROM tokens",
        "INSERT INTO collections (name) VALUES ('pirate')",
        "DELETE FROM documents",
        "DROP TABLE chunks",
    ],
)
def test_reader_cannot_see_secrets_or_write(as_reader, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        as_reader(statement)
