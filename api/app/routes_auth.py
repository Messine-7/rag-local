"""Connexion par mot de passe, déconnexion et clés API personnelles (phase 5.1)."""

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

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
    async with request.app.state.pool.connection() as conn:
        user = await authenticate(conn, req.username.strip().lower(), req.password)
        if user is None:
            raise HTTPException(status_code=401, detail="identifiant ou mot de passe incorrect")
        issued = await issue_token(
            conn, user.id, kind="session", ttl=timedelta(hours=settings.session_ttl_hours)
        )
    return TokenOut(token=issued.token, expires_at=issued.expires_at)


@router.post("/logout", status_code=204)
async def logout(caller: CurrentCaller, request: Request) -> None:
    """Révoque le jeton utilisé pour cette requête (session ou clé API)."""
    async with request.app.state.pool.connection() as conn:
        await revoke_token(conn, caller.user.id, caller.token_id)


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    """L'utilisateur auquel appartient le jeton."""
    return UserOut(id=user.id, username=user.username, is_admin=user.is_admin)


@router.post("/api-keys", status_code=201)
async def create_api_key(req: ApiKeyRequest, user: CurrentUser, request: Request) -> ApiKeyCreated:
    """Crée une clé API personnelle (CLI, scripts), valable jusqu'à sa révocation.
    Sa valeur n'est montrée qu'ici : la noter tout de suite."""
    async with request.app.state.pool.connection() as conn:
        issued = await issue_token(conn, user.id, kind="api_key", name=req.name)
    return ApiKeyCreated(id=issued.id, name=req.name, token=issued.token, expires_at=None)


@router.get("/api-keys")
async def api_keys(user: CurrentUser, request: Request) -> list[ApiKeyOut]:
    """Ses clés API encore valables (sans leur valeur, seulement leur début)."""
    async with request.app.state.pool.connection() as conn:
        return [ApiKeyOut(**key.__dict__) for key in await list_api_keys(conn, user.id)]


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(key_id: int, user: CurrentUser, request: Request) -> None:
    """Révoque une de ses clés API."""
    async with request.app.state.pool.connection() as conn:
        if not await revoke_token(conn, user.id, key_id):
            raise HTTPException(status_code=404, detail="clé inconnue ou déjà révoquée")
