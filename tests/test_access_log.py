"""Journal d'accès (phase 5.5) : ce qui est tracé, qui peut le lire, et le fait que l'API ne
peut ni le modifier ni l'effacer. Les questions (POST /ask) sont testées dans test_ask.py.

Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

import psycopg
import pytest

from app.auth import create_user
from conftest import ADMIN_USER, TEST_USER, login_headers
from test_governance import api_query, make_pdf


def log(sql, action: str | None = None) -> list[tuple]:
    """(action, nom, detail) dans l'ordre, lus par le propriétaire de la base."""
    where = "WHERE l.action = %s" if action else ""
    return sql(
        "SELECT l.action, u.username, l.detail FROM access_log l"
        f" LEFT JOIN users u ON u.id = l.user_id {where} ORDER BY l.id",
        (action,) if action else (),
    )


@pytest.fixture
def bob(anon_client, run_db) -> dict[str, str]:
    run_db(lambda conn: create_user(conn, "bob", "mot-de-passe-bob"))
    return login_headers(anon_client, "bob", "mot-de-passe-bob")


# --- Ce qui est tracé -------------------------------------------------------------------


def test_logins_success_and_failure(anon_client, bob, sql):
    refused = anon_client.post(
        "/auth/login",
        json={"username": "Bob", "password": "mauvais-mot-de-passe"},
        headers={"X-Forwarded-For": "100.101.102.103, 127.0.0.1"},
    )
    assert refused.status_code == 401
    rows = sql("SELECT action, user_id, token_id IS NOT NULL, client, detail FROM access_log")
    assert rows == [
        ("login", rows[0][1], True, "testclient", {}),
        ("login_failed", None, False, "100.101.102.103", {"username": "bob"}),
    ]
    # Jamais le mot de passe tenté
    assert "mauvais" not in str(sql("SELECT detail::text FROM access_log"))


def test_api_keys_and_logout(anon_client, bob, sql):
    key = anon_client.post("/auth/api-keys", json={"name": "laptop"}, headers=bob).json()
    assert anon_client.delete(f"/auth/api-keys/{key['id']}", headers=bob).status_code == 204
    # Une révocation refusée ne laisse pas de ligne (la transaction est annulée)
    assert anon_client.delete("/auth/api-keys/99999", headers=bob).status_code == 404
    assert anon_client.post("/auth/logout", headers=bob).status_code == 204
    assert log(sql)[1:] == [
        ("api_key_create", "bob", {"key_id": key["id"], "name": "laptop"}),
        ("api_key_revoke", "bob", {"key_id": key["id"]}),
        ("logout", "bob", {}),
    ]
    # Aucune valeur de jeton dans le journal
    assert key["token"] not in str(sql("SELECT detail::text FROM access_log"))


def test_document_upload(client, sql):
    pdf = make_pdf("rapport annuel")
    files = {"file": ("doc.pdf", pdf, "application/pdf")}
    first = client.post("/documents", files=files, data={"collection": "défaut"}).json()
    client.post("/documents", files=files, data={"collection": "défaut"})  # déjà indexé
    rows = log(sql, "document_upload")
    assert [(who, d["document_id"], d["collection"], d["queued"]) for _, who, d in rows] == [
        (TEST_USER, first["id"], "défaut", True),
        (TEST_USER, first["id"], "défaut", False),
    ]
    assert rows[0][2]["filename"] == "doc.pdf"


def test_admin_actions(anon_client, admin_headers, bob, sql):
    c, h = anon_client, admin_headers
    coll = c.post("/admin/collections", json={"name": "rh"}, headers=h).json()
    group = c.post("/admin/groups", json={"name": "équipe-rh"}, headers=h).json()
    bob_id = sql("SELECT id FROM users WHERE username = 'bob'")[0][0]
    c.put(f"/admin/groups/{group['id']}/members/{bob_id}", headers=h)
    c.put(
        f"/admin/groups/{group['id']}/collections/{coll['id']}",
        json={"permission": "write"},
        headers=h,
    )
    c.delete(f"/admin/groups/{group['id']}/collections/{coll['id']}", headers=h)
    c.delete(f"/admin/groups/{group['id']}/members/{bob_id}", headers=h)
    c.patch(f"/admin/users/{bob_id}", json={"disabled": True}, headers=h)
    c.delete(f"/admin/groups/{group['id']}", headers=h)
    rows = log(sql, "admin")
    assert {who for _, who, _ in rows} == {ADMIN_USER}
    assert [d["op"] for _, _, d in rows] == [
        "collection_create",
        "group_create",
        "member_add",
        "grant",
        "revoke",
        "member_remove",
        "user_update",
        "group_delete",
    ]
    assert rows[3][2] == {
        "op": "grant",
        "group_id": group["id"],
        "collection_id": coll["id"],
        "permission": "write",
    }
    assert rows[6][2] == {"op": "user_update", "user_id": bob_id, "disabled": True}


# --- Lecture : admins seulement ---------------------------------------------------------


def test_only_admins_read_the_log(anon_client, admin_headers, bob):
    assert anon_client.get("/admin/access-log", headers=bob).status_code == 403
    entries = anon_client.get("/admin/access-log", headers=admin_headers).json()
    # Du plus récent au plus ancien : connexion de bob, puis celle de l'admin
    assert [(e["action"], e["username"]) for e in entries] == [
        ("login", "bob"),
        ("login", ADMIN_USER),
    ]
    only_admin = anon_client.get(
        "/admin/access-log",
        params={"user_id": entries[1]["user_id"], "action": "login"},
        headers=admin_headers,
    ).json()
    assert [e["username"] for e in only_admin] == [ADMIN_USER]
    page = anon_client.get("/admin/access-log", params={"limit": 1}, headers=admin_headers).json()
    older = anon_client.get(
        "/admin/access-log", params={"before_id": page[0]["id"]}, headers=admin_headers
    ).json()
    assert [e["id"] for e in page + older] == [e["id"] for e in entries]


# --- Dans Postgres, sous le rôle rag_api (comme le ferait une injection SQL) ------------


def test_rag_api_cannot_alter_or_erase_the_log(anon_client, admin_headers, db_url):
    for statement in ("UPDATE access_log SET action = 'logout'", "DELETE FROM access_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api_query(db_url, admin_headers, statement)


def test_non_admin_cannot_read_the_log_in_postgres(anon_client, admin_headers, bob, db_url):
    assert api_query(db_url, bob, "SELECT count(*) FROM access_log") == [(0,)]
    assert api_query(db_url, admin_headers, "SELECT count(*) FROM access_log") == [(2,)]


def test_cannot_write_in_someone_elses_name(anon_client, admin_headers, bob, db_url, sql):
    admin_id = sql("SELECT id FROM users WHERE username = %s", (ADMIN_USER,))[0][0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_query(
            db_url,
            bob,
            "INSERT INTO access_log (user_id, action) VALUES (%s, 'admin')",
            (admin_id,),
        )


def test_cli_commands_are_logged(anon_client, sql):
    import argparse
    import asyncio

    from app.admin import run

    for command, extra in (
        ("create-user", {"admin": False, "no_password": True}),
        ("create-api-key", {"name": "serveur"}),
        ("list-users", {}),
    ):
        asyncio.run(run(argparse.Namespace(command=command, username="dave", **extra)))
    assert sql("SELECT action, user_id, detail FROM access_log ORDER BY id") == [
        ("admin", None, {"op": "create-user", "username": "dave", "via": "cli"}),
        (
            "admin",
            None,
            {"op": "create-api-key", "username": "dave", "via": "cli", "name": "serveur"},
        ),
    ]
