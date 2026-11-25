"""Accès à PostgreSQL : migrations SQL écrites à la main, et format des vecteurs pgvector.

Migrations : fichiers app/migrations/NNN_nom.sql, appliqués dans l'ordre au démarrage de
l'API et du worker. La table schema_migrations garde ceux qui sont déjà passés ; un verrou
consultatif empêche l'API et le worker de les appliquer en même temps.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
MIGRATION_LOCK_ID = 4_210_001  # identifiant arbitraire du verrou consultatif


async def migrate(conn: psycopg.AsyncConnection) -> list[str]:
    """Applique les migrations manquantes, toutes dans une seule transaction.

    Renvoie les noms des fichiers appliqués (liste vide si la base était à jour).
    """
    applied: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in await cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            # Sans paramètres, psycopg envoie le fichier tel quel : plusieurs instructions permises
            await conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
    if applied:
        log.info("migrations appliquées : %s", ", ".join(applied))
    return applied


async def set_api_password(conn: psycopg.AsyncConnection, password: str) -> None:
    """Ouvre la connexion du rôle rag_api (migration 004) avec le mot de passe de .env.

    Exécuté par le propriétaire à chaque démarrage de l'API : changer API_DB_PASSWORD dans
    .env puis redémarrer suffit."""
    await conn.execute(
        sql.SQL("ALTER ROLE rag_api WITH LOGIN PASSWORD {}").format(sql.Literal(password))
    )


async def check_api_connection(conn: psycopg.AsyncConnection) -> None:
    """Refuse de démarrer si l'API n'est pas soumise à la Row-Level Security, ou si la base
    n'est pas à jour (migration manquante)."""
    cur = await conn.execute(
        "SELECT current_user, rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    row = await cur.fetchone()
    assert row is not None
    if row[1]:
        raise RuntimeError(
            f"l'API est connectée en tant que {row[0]}, qui contourne la Row-Level Security :"
            " API_DATABASE_URL doit utiliser le rôle rag_api"
        )
    cur = await conn.execute("SELECT version FROM schema_migrations")
    done = {r[0] for r in await cur.fetchall()}
    missing = [p.name for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in done]
    if missing:
        raise RuntimeError(f"migrations non appliquées : {', '.join(missing)}")


def to_vector(values: Sequence[float]) -> str:
    """Vecteur au format texte de pgvector ('[0.1,0.2,…]'), à caster en ::vector dans le SQL."""
    return "[" + ",".join(f"{x:.7g}" for x in values) + "]"
