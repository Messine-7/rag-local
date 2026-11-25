"""Accès à PostgreSQL : migrations SQL écrites à la main, et format des vecteurs pgvector.

Migrations : fichiers app/migrations/NNN_nom.sql, appliqués dans l'ordre au démarrage de
l'API et du worker. La table schema_migrations garde ceux qui sont déjà passés ; un verrou
consultatif empêche l'API et le worker de les appliquer en même temps.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

import psycopg

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


def to_vector(values: Sequence[float]) -> str:
    """Vecteur au format texte de pgvector ('[0.1,0.2,…]'), à caster en ::vector dans le SQL."""
    return "[" + ",".join(f"{x:.7g}" for x in values) + "]"
