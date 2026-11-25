"""Connexion : utilisateurs, mots de passe, jetons de session et clés API (phase 5.1).

- Mot de passe haché avec argon2 (lent exprès : décourage les essais en masse).
- Jetons aléatoires de 256 bits : seule leur empreinte SHA-256 est en base, un hachage lent
  serait inutile pour un secret aussi long.
- Chaque requête envoie « Authorization: Bearer <jeton> » ; current_user() le vérifie.
"""

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool

SESSION_PREFIX = "rag_s_"
API_KEY_PREFIX = "rag_k_"
MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()
# Vérifié quand l'utilisateur n'existe pas : la réponse prend le même temps, ce qui ne révèle
# pas quels noms d'utilisateur existent.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class User:
    id: int
    username: str
    is_admin: bool


@dataclass(frozen=True)
class IssuedToken:
    """Jeton qui vient d'être créé : sa valeur n'est montrée qu'une fois."""

    id: int
    token: str
    expires_at: datetime | None


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str | None
    prefix: str
    created_at: datetime
    last_used_at: datetime | None


class UserExists(Exception):
    pass


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"mot de passe trop court (au moins {MIN_PASSWORD_LENGTH} caractères)")
    return _hasher.hash(password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_user(
    conn: psycopg.AsyncConnection, username: str, password: str | None, *, is_admin: bool = False
) -> User:
    password_hash = hash_password(password) if password is not None else None
    try:
        async with conn.cursor(row_factory=class_row(User)) as cur:
            await cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s)"
                " RETURNING id, username, is_admin",
                (username, password_hash, is_admin),
            )
            user = await cur.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise UserExists(username) from exc
    assert user is not None
    return user


async def authenticate(conn: psycopg.AsyncConnection, username: str, password: str) -> User | None:
    """Vérifie nom et mot de passe ; None si l'un des deux est faux ou le compte désactivé."""
    cur = await conn.execute(
        "SELECT id, username, is_admin, password_hash FROM users"
        " WHERE username = %s AND disabled_at IS NULL",
        (username,),
    )
    row = await cur.fetchone()
    stored = row[3] if row and row[3] else _DUMMY_HASH
    try:
        _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return None
    if row is None or row[3] is None:
        return None
    # Pas de mise à jour du hachage ici : l'API (rôle rag_api) ne peut pas modifier un compte
    # sans être admin (RLS). Nouveaux paramètres argon2 : au prochain changement de mot de passe.
    return User(id=row[0], username=row[1], is_admin=row[2])


async def issue_token(
    conn: psycopg.AsyncConnection,
    user_id: int,
    *,
    kind: str,
    name: str | None = None,
    ttl: timedelta | None = None,
) -> IssuedToken:
    """Crée un jeton de session (kind="session", avec ttl) ou une clé API (kind="api_key")."""
    token = (SESSION_PREFIX if kind == "session" else API_KEY_PREFIX) + secrets.token_urlsafe(32)
    cur = await conn.execute(
        "INSERT INTO tokens (user_id, kind, name, token_hash, prefix, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, now() + %s) RETURNING id, expires_at",
        (user_id, kind, name, _token_hash(token), token[:12], ttl),
    )
    row = await cur.fetchone()
    assert row is not None
    return IssuedToken(id=row[0], token=token, expires_at=row[1])


async def user_for_token(conn: psycopg.AsyncConnection, token: str) -> tuple[User, int] | None:
    """Utilisateur et id du jeton, si le jeton est valable (ni expiré, ni révoqué, compte actif).

    Passe par la fonction rag_token_user (migration 004) : l'API ne peut pas lire les
    empreintes des jetons elle-même."""
    cur = await conn.execute(
        "SELECT user_id, username, is_admin, token_id FROM rag_token_user(%s)",
        (_token_hash(token),),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return User(id=row[0], username=row[1], is_admin=row[2]), row[3]


async def revoke_token(conn: psycopg.AsyncConnection, user_id: int, token_id: int) -> bool:
    cur = await conn.execute(
        "UPDATE tokens SET revoked_at = now()"
        " WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
        (token_id, user_id),
    )
    return cur.rowcount == 1


async def list_api_keys(conn: psycopg.AsyncConnection, user_id: int) -> list[ApiKey]:
    async with conn.cursor(row_factory=class_row(ApiKey)) as cur:
        await cur.execute(
            "SELECT id, name, prefix, created_at, last_used_at FROM tokens"
            " WHERE user_id = %s AND kind = 'api_key' AND revoked_at IS NULL ORDER BY id",
            (user_id,),
        )
        return await cur.fetchall()


# --- Dépendances FastAPI ---------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Caller:
    """Qui appelle l'API, et avec quel jeton (déconnexion, droits dans Postgres)."""

    user: User
    token_id: int
    token_hash: str


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def current_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    """Exige un jeton valable dans l'en-tête Authorization, sinon 401."""
    if credentials is None:
        raise _unauthorized("authentification requise (Authorization: Bearer <jeton>)")
    async with request.app.state.pool.connection() as conn:
        found = await user_for_token(conn, credentials.credentials)
    if found is None:
        raise _unauthorized("jeton invalide, expiré ou révoqué")
    user, token_id = found
    return Caller(user=user, token_id=token_id, token_hash=_token_hash(credentials.credentials))


async def current_user(caller: Annotated[Caller, Depends(current_caller)]) -> User:
    return caller.user


@asynccontextmanager
async def caller_connection(
    pool: AsyncConnectionPool, token_hash: str
) -> AsyncIterator[psycopg.AsyncConnection]:
    """Connexion dans une transaction où Postgres sait qui appelle (Row-Level Security).

    L'empreinte du jeton est placée dans app.token_hash, pour cette transaction seulement
    (set_config(…, true)) : la connexion rendue au pool ne garde rien."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.token_hash', %s, true)", (token_hash,))
        yield conn


async def caller_db(
    request: Request, caller: Annotated[Caller, Depends(current_caller)]
) -> AsyncIterator[psycopg.AsyncConnection]:
    """Dépendance FastAPI : connexion soumise aux droits de l'appelant, pour toute la requête."""
    async with caller_connection(request.app.state.pool, caller.token_hash) as conn:
        yield conn


async def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="réservé aux administrateurs")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
CurrentCaller = Annotated[Caller, Depends(current_caller)]
AdminUser = Annotated[User, Depends(admin_user)]
# scope="function" : transaction validée AVANT l'envoi de la réponse (sinon un client qui
# enchaîne deux requêtes pourrait ne pas voir ce qu'il vient d'écrire)
CallerDB = Annotated[psycopg.AsyncConnection, Depends(caller_db, scope="function")]
