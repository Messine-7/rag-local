"""Journal d'accès (phase 5.5) : table access_log, migration 007.

Une ligne par action : question, connexion, envoi de document, action d'administration.
Métadonnées seulement (qui, quand, quel jeton, quel modèle, quels chunks et documents) :
jamais le texte des questions ni des réponses, jamais de mot de passe ni de jeton.

La ligne est écrite dans la même transaction que l'action : si elle ne peut pas être
écrite, l'action échoue aussi (pas d'accès sans trace). L'API ne peut ni modifier ni effacer
le journal ; seuls les admins le lisent (GET /admin/access-log).
"""

from dataclasses import dataclass
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Request
from psycopg.types.json import Jsonb

from app.auth import CallerDB, CurrentCaller


def client_address(request: Request) -> str | None:
    """Adresse de l'appelant. Derrière tailscale serve, l'API voit 127.0.0.1 : l'adresse
    d'origine est dans X-Forwarded-For (premier élément)."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    return request.client.host if request.client else None


async def record(
    conn: psycopg.AsyncConnection,
    action: str,
    *,
    user_id: int | None,
    token_id: int | None = None,
    client: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    # Sans RETURNING : rag_api ne lit pas le journal (sauf les admins)
    await conn.execute(
        "INSERT INTO access_log (user_id, token_id, action, client, detail)"
        " VALUES (%s, %s, %s, %s, %s)",
        (user_id, token_id, action, client, Jsonb(detail or {})),
    )


@dataclass(frozen=True)
class Auditor:
    """Journal lié à la requête : connexion, appelant et adresse déjà connus."""

    conn: psycopg.AsyncConnection
    user_id: int
    token_id: int
    client: str | None

    async def __call__(self, action: str, **detail: Any) -> None:
        await record(
            self.conn,
            action,
            user_id=self.user_id,
            token_id=self.token_id,
            client=self.client,
            detail=detail,
        )


async def auditor(request: Request, conn: CallerDB, caller: CurrentCaller) -> Auditor:
    """Même connexion (et même transaction) que la route : FastAPI ne crée CallerDB qu'une
    fois par requête."""
    return Auditor(conn, caller.user.id, caller.token_id, client_address(request))


Audit = Annotated[Auditor, Depends(auditor)]
