"""Connexion par mot de passe, déconnexion et clés API personnelles (phase 5.1).

Chaque connexion (réussie ou non), déconnexion, création ou révocation de clé API est
inscrite au journal d'accès (phase 5.5, app.audit)."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.audit import Audit, client_address, record
from app.auth import (
    CurrentCaller,
    CurrentUser,
    authenticate,
    issue_token,
    list_api_keys,
    revoke_token,
)
from app.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["connexion"])

Config = Annotated[Settings, Depends(get_settings)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class TokenOut(BaseModel):
    token: str  # à envoyer dans « Authorization: Bearer <token> » ; montré une seule fois
    expires_at: datetime | None


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool


class ApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Pour la reconnaître : « laptop »")


class ApiKeyCreated(TokenOut):
    id: int
    name: str


class ApiKeyOut(BaseModel):
    id: int
    name: str | None
    prefix: str
    created_at: datetime
    last_used_at: datetime | None


@router.post("/login")
async def login(req: LoginRequest, request: Request, settings: Config) -> TokenOut:
    """Échange nom d'utilisateur et mot de passe contre un jeton de session."""
    username = req.username.strip().lower()
    client = client_address(request)
    async with request.app.state.pool.connection() as conn:
        user = await authenticate(conn, username, req.password)
        if user is None:
            # Écrit et validé avant le refus (une exception dans ce bloc annulerait la ligne)
            await record(
                conn, "login_failed", user_id=None, client=client, detail={"username": username}
            )
        else:
            issued = await issue_token(
                conn, user.id, kind="session", ttl=timedelta(hours=settings.session_ttl_hours)
            )
            await record(conn, "login", user_id=user.id, token_id=issued.id, client=client)
    if user is None:
        raise HTTPException(status_code=401, detail="identifiant ou mot de passe incorrect")
    return TokenOut(token=issued.token, expires_at=issued.expires_at)


@router.post("/logout", status_code=204)
async def logout(caller: CurrentCaller, audit: Audit) -> None:
    """Révoque le jeton utilisé pour cette requête (session ou clé API)."""
    await audit("logout")  # avant la révocation : ensuite, Postgres ne reconnaît plus l'appelant
    await revoke_token(audit.conn, caller.user.id, caller.token_id)


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    """L'utilisateur auquel appartient le jeton."""
    return UserOut(id=user.id, username=user.username, is_admin=user.is_admin)


@router.post("/api-keys", status_code=201)
async def create_api_key(req: ApiKeyRequest, user: CurrentUser, audit: Audit) -> ApiKeyCreated:
    """Crée une clé API personnelle (CLI, scripts), valable jusqu'à sa révocation.
    Sa valeur n'est montrée qu'ici : la noter tout de suite."""
    issued = await issue_token(audit.conn, user.id, kind="api_key", name=req.name)
    await audit("api_key_create", key_id=issued.id, name=req.name)
    return ApiKeyCreated(id=issued.id, name=req.name, token=issued.token, expires_at=None)


@router.get("/api-keys")
async def api_keys(user: CurrentUser, request: Request) -> list[ApiKeyOut]:
    """Ses clés API encore valables (sans leur valeur, seulement leur début)."""
    async with request.app.state.pool.connection() as conn:
        return [ApiKeyOut(**key.__dict__) for key in await list_api_keys(conn, user.id)]


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(key_id: int, caller: CurrentCaller, audit: Audit) -> None:
    """Révoque une de ses clés API."""
    # Écrit avant, comme pour /logout (la clé révoquée peut être celle de cette requête) ;
    # la 404 annule la transaction, et la ligne avec.
    await audit("api_key_revoke", key_id=key_id)
    if not await revoke_token(audit.conn, caller.user.id, key_id):
        raise HTTPException(status_code=404, detail="clé inconnue ou déjà révoquée")
