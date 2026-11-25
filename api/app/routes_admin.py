"""Administration (phase 5.2) : comptes, collections, groupes et droits groupe → collection.

Réservé aux admins, deux fois : l'API refuse (403) un non-admin, et Postgres refuse de toute
façon les écritures sur ces tables à qui n'est pas admin (Row-Level Security, migration 004).
"""

from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from app.auth import AdminUser, CallerDB, UserExists, create_user

router = APIRouter(prefix="/admin", tags=["administration"])

Id = Annotated[int, Path(ge=1)]
Permission = Literal["read", "write"]


# --- Comptes ----------------------------------------------------------------------------


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    password: str | None = Field(
        default=None, min_length=10, max_length=1024, description="Absent : clé API seulement"
    )
    is_admin: bool = False


class UserPatch(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


class AdminUserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    disabled: bool
    groups: list[str]
    created_at: datetime


_USERS = """
SELECT u.id, u.username, u.is_admin, u.disabled_at IS NOT NULL AS disabled,
       coalesce(array_agg(g.name ORDER BY g.name) FILTER (WHERE g.id IS NOT NULL), '{}') AS groups,
       u.created_at
FROM users u
LEFT JOIN group_members m ON m.user_id = u.id
LEFT JOIN groups g ON g.id = m.group_id
"""


async def _user(conn: psycopg.AsyncConnection, user_id: int) -> AdminUserOut:
    cur = await conn.execute(_USERS + " WHERE u.id = %s GROUP BY u.id", (user_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="utilisateur inconnu")
    return AdminUserOut(**dict(zip(AdminUserOut.model_fields, row, strict=True)))


@router.get("/users")
async def list_users(conn: CallerDB) -> list[AdminUserOut]:
    cur = await conn.execute(_USERS + " GROUP BY u.id ORDER BY u.username")
    fields = list(AdminUserOut.model_fields)
    return [AdminUserOut(**dict(zip(fields, row, strict=True))) for row in await cur.fetchall()]


@router.post("/users", status_code=201)
async def add_user(req: UserCreate, conn: CallerDB) -> AdminUserOut:
    """Crée un compte. Sans mot de passe, il ne sert qu'avec une clé API."""
    try:
        user = await create_user(conn, req.username, req.password, is_admin=req.is_admin)
    except UserExists as exc:
        raise HTTPException(status_code=409, detail="ce nom d'utilisateur existe déjà") from exc
    return await _user(conn, user.id)


@router.patch("/users/{user_id}")
async def change_user(user_id: Id, req: UserPatch, conn: CallerDB, me: AdminUser) -> AdminUserOut:
    """Donne ou retire les droits d'admin, désactive ou réactive un compte."""
    if user_id == me.id and (req.is_admin is False or req.disabled):
        # Évite de se retrouver sans aucun admin pour réparer
        raise HTTPException(status_code=400, detail="impossible sur son propre compte")
    await _user(conn, user_id)  # 404 si inconnu
    if req.is_admin is not None:
        await conn.execute("UPDATE users SET is_admin = %s WHERE id = %s", (req.is_admin, user_id))
    if req.disabled is not None:
        await conn.execute(
            "UPDATE users SET disabled_at = CASE WHEN %s THEN now() END WHERE id = %s",
            (req.disabled, user_id),
        )
    return await _user(conn, user_id)


# --- Collections ------------------------------------------------------------------------


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class AdminCollectionOut(BaseModel):
    id: int
    name: str
    documents: int
    created_at: datetime


@router.get("/collections")
async def list_all_collections(conn: CallerDB) -> list[AdminCollectionOut]:
    cur = await conn.execute(
        "SELECT c.id, c.name, count(d.id)::int, c.created_at FROM collections c"
        " LEFT JOIN documents d ON d.collection_id = c.id GROUP BY c.id ORDER BY c.name"
    )
    fields = list(AdminCollectionOut.model_fields)
    return [AdminCollectionOut(**dict(zip(fields, r, strict=True))) for r in await cur.fetchall()]


@router.post("/collections", status_code=201)
async def add_collection(req: CollectionCreate, conn: CallerDB) -> AdminCollectionOut:
    try:
        cur = await conn.execute(
            "INSERT INTO collections (name) VALUES (%s) RETURNING id, name, 0, created_at",
            (req.name.strip(),),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="cette collection existe déjà") from exc
    row = await cur.fetchone()
    return AdminCollectionOut(**dict(zip(AdminCollectionOut.model_fields, row, strict=True)))


# --- Groupes et droits ------------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class GrantIn(BaseModel):
    permission: Permission


class GrantOut(BaseModel):
    collection_id: int
    collection: str
    permission: Permission


class GroupOut(BaseModel):
    id: int
    name: str
    members: list[str]
    collections: list[GrantOut]


async def _groups(conn: psycopg.AsyncConnection, group_id: int | None = None) -> list[GroupOut]:
    where = "WHERE g.id = %s" if group_id is not None else ""
    cur = await conn.execute(
        f"""
        SELECT g.id, g.name,
          coalesce((SELECT array_agg(u.username ORDER BY u.username) FROM group_members m
                    JOIN users u ON u.id = m.user_id WHERE m.group_id = g.id), '{{}}'),
          coalesce((SELECT json_agg(json_build_object('collection_id', c.id,
                        'collection', c.name, 'permission', cg.permission) ORDER BY c.name)
                    FROM collection_grants cg JOIN collections c ON c.id = cg.collection_id
                    WHERE cg.group_id = g.id), '[]')
        FROM groups g {where} ORDER BY g.name
        """,
        (group_id,) if group_id is not None else (),
    )
    return [
        GroupOut(id=r[0], name=r[1], members=r[2], collections=r[3]) for r in await cur.fetchall()
    ]


async def _group(conn: psycopg.AsyncConnection, group_id: int) -> GroupOut:
    found = await _groups(conn, group_id)
    if not found:
        raise HTTPException(status_code=404, detail="groupe inconnu")
    return found[0]


@router.get("/groups")
async def list_groups(conn: CallerDB) -> list[GroupOut]:
    """Groupes, avec leurs membres et leurs droits sur les collections."""
    return await _groups(conn)


@router.post("/groups", status_code=201)
async def add_group(req: GroupCreate, conn: CallerDB) -> GroupOut:
    try:
        cur = await conn.execute(
            "INSERT INTO groups (name) VALUES (%s) RETURNING id", (req.name.strip(),)
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="ce groupe existe déjà") from exc
    row = await cur.fetchone()
    assert row is not None
    return await _group(conn, row[0])


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: Id, conn: CallerDB) -> None:
    """Supprime le groupe, ses appartenances et ses droits (pas les comptes ni les documents)."""
    cur = await conn.execute("DELETE FROM groups WHERE id = %s", (group_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="groupe inconnu")


@router.put("/groups/{group_id}/members/{user_id}")
async def add_member(group_id: Id, user_id: Id, conn: CallerDB) -> GroupOut:
    try:
        await conn.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (group_id, user_id),
        )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="groupe ou utilisateur inconnu") from exc
    return await _group(conn, group_id)


@router.delete("/groups/{group_id}/members/{user_id}")
async def remove_member(group_id: Id, user_id: Id, conn: CallerDB) -> GroupOut:
    cur = await conn.execute(
        "DELETE FROM group_members WHERE group_id = %s AND user_id = %s", (group_id, user_id)
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="cet utilisateur n'est pas dans ce groupe")
    return await _group(conn, group_id)


@router.put("/groups/{group_id}/collections/{collection_id}")
async def grant(group_id: Id, collection_id: Id, req: GrantIn, conn: CallerDB) -> GroupOut:
    """Donne (ou change) le droit du groupe sur une collection : read ou write."""
    try:
        await conn.execute(
            "INSERT INTO collection_grants (group_id, collection_id, permission)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (group_id, collection_id) DO UPDATE SET permission = EXCLUDED.permission",
            (group_id, collection_id, req.permission),
        )
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=404, detail="groupe ou collection inconnu") from exc
    return await _group(conn, group_id)


@router.delete("/groups/{group_id}/collections/{collection_id}")
async def revoke(group_id: Id, collection_id: Id, conn: CallerDB) -> GroupOut:
    cur = await conn.execute(
        "DELETE FROM collection_grants WHERE group_id = %s AND collection_id = %s",
        (group_id, collection_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="aucun droit de ce groupe sur cette collection")
    return await _group(conn, group_id)
