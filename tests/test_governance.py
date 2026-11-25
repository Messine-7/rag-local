"""Gouvernance (phase 5.2) : groupes, droits par collection, routes d'administration, et
Row-Level Security vérifiée directement dans Postgres (sous le rôle rag_api de l'API).

Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

import hashlib

import psycopg
import pymupdf
import pytest
from psycopg.conninfo import make_conninfo

from app.auth import create_user
from app.db import check_api_connection
from conftest import API_DB_PASSWORD, login_headers

VECTOR = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"


def make_pdf(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), text)
    return doc.tobytes()


def upload(c, collection: str, text: str, headers=None):
    return c.post(
        "/documents",
        files={"file": ("doc.pdf", make_pdf(text), "application/pdf")},
        data={"collection": collection},
        headers=headers,
    )


@pytest.fixture
def setup(anon_client, admin_headers, run_db):
    """Collections « rh » et « tech » créées par l'admin via l'API ; bob (lecture sur tech)
    et carol (aucun groupe). Renvoie un petit carnet d'adresses."""
    c = anon_client
    rh = c.post("/admin/collections", json={"name": "rh"}, headers=admin_headers).json()
    tech = c.post("/admin/collections", json={"name": "tech"}, headers=admin_headers).json()
    bob = c.post(
        "/admin/users",
        json={"username": "bob", "password": "mot-de-passe-bob"},
        headers=admin_headers,
    ).json()
    run_db(lambda conn: create_user(conn, "carol", "mot-de-passe-carol"))
    group = c.post("/admin/groups", json={"name": "lecteurs-tech"}, headers=admin_headers).json()
    c.put(f"/admin/groups/{group['id']}/members/{bob['id']}", headers=admin_headers)
    granted = c.put(
        f"/admin/groups/{group['id']}/collections/{tech['id']}",
        json={"permission": "read"},
        headers=admin_headers,
    )
    assert granted.status_code == 200, granted.text
    # Un document dans chaque collection, envoyé par l'admin
    doc_rh = upload(c, "rh", "salaires", headers=admin_headers).json()
    doc_tech = upload(c, "tech", "architecture", headers=admin_headers).json()
    return {
        "admin": admin_headers,
        "bob": login_headers(c, "bob", "mot-de-passe-bob"),
        "carol": login_headers(c, "carol", "mot-de-passe-carol"),
        "rh": rh,
        "tech": tech,
        "group": group,
        "doc_rh": doc_rh,
        "doc_tech": doc_tech,
    }


# --- Ce que voit chacun, à travers l'API ------------------------------------------------


def test_admin_sees_everything(anon_client, setup):
    docs = anon_client.get("/documents", headers=setup["admin"]).json()
    assert {d["collection"] for d in docs} == {"rh", "tech"}
    names = {
        c["name"]: c["permission"]
        for c in anon_client.get("/collections", headers=setup["admin"]).json()
    }
    assert names == {"défaut": "write", "rh": "write", "tech": "write"}


def test_reader_sees_only_his_collection(anon_client, setup):
    bob = setup["bob"]
    assert [d["id"] for d in anon_client.get("/documents", headers=bob).json()] == [
        setup["doc_tech"]["id"]
    ]
    assert anon_client.get("/collections", headers=bob).json() == [
        {"id": setup["tech"]["id"], "name": "tech", "permission": "read"}
    ]
    # Document d'une autre collection : 404, son existence n'est pas révélée
    assert anon_client.get(f"/documents/{setup['doc_rh']['id']}", headers=bob).status_code == 404


def test_reader_cannot_upload(anon_client, setup, tmp_path):
    response = upload(anon_client, "tech", "nouveau", headers=setup["bob"])
    assert response.status_code == 403
    # Refus avant d'écrire sur le disque : seuls les deux PDF de l'admin sont là
    assert len(list(tmp_path.glob("*.pdf"))) == 2
    # Collection invisible : même réponse qu'une collection qui n'existe pas
    assert upload(anon_client, "rh", "x", headers=setup["bob"]).status_code == 404
    assert upload(anon_client, "inexistante", "x", headers=setup["bob"]).status_code == 404


def test_user_without_group_sees_nothing(anon_client, setup):
    carol = setup["carol"]
    assert anon_client.get("/documents", headers=carol).json() == []
    assert anon_client.get("/collections", headers=carol).json() == []


def test_write_permission_allows_upload(anon_client, setup):
    a = setup["admin"]
    anon_client.put(
        f"/admin/groups/{setup['group']['id']}/collections/{setup['tech']['id']}",
        json={"permission": "write"},
        headers=a,
    )
    assert upload(anon_client, "tech", "par bob", headers=setup["bob"]).status_code == 202


def test_removing_member_removes_access(anon_client, setup):
    group_id, bob_id = (
        setup["group"]["id"],
        anon_client.get("/auth/me", headers=setup["bob"]).json()["id"],
    )
    removed = anon_client.delete(
        f"/admin/groups/{group_id}/members/{bob_id}", headers=setup["admin"]
    )
    assert removed.json()["members"] == []
    assert anon_client.get("/documents", headers=setup["bob"]).json() == []


# --- Administration ---------------------------------------------------------------------


def test_admin_routes_refused_to_non_admins(anon_client, setup):
    for method, path in [
        ("get", "/admin/users"),
        ("post", "/admin/collections"),
        ("get", "/admin/groups"),
    ]:
        response = anon_client.request(method, path, headers=setup["bob"], json={"name": "x"})
        assert response.status_code == 403


def test_admin_lists_groups_and_users(anon_client, setup):
    groups = anon_client.get("/admin/groups", headers=setup["admin"]).json()
    assert groups == [
        {
            "id": setup["group"]["id"],
            "name": "lecteurs-tech",
            "members": ["bob"],
            "collections": [
                {"collection_id": setup["tech"]["id"], "collection": "tech", "permission": "read"}
            ],
        }
    ]
    users = {
        u["username"]: u for u in anon_client.get("/admin/users", headers=setup["admin"]).json()
    }
    assert users["bob"]["groups"] == ["lecteurs-tech"]
    assert users["root"]["is_admin"] is True


def test_admin_cannot_lock_himself_out(anon_client, setup):
    me = anon_client.get("/auth/me", headers=setup["admin"]).json()
    for body in ({"is_admin": False}, {"disabled": True}):
        assert (
            anon_client.patch(
                f"/admin/users/{me['id']}", json=body, headers=setup["admin"]
            ).status_code
            == 400
        )


def test_disable_user_through_admin(anon_client, setup):
    bob_id = anon_client.get("/auth/me", headers=setup["bob"]).json()["id"]
    patched = anon_client.patch(
        f"/admin/users/{bob_id}", json={"disabled": True}, headers=setup["admin"]
    )
    assert patched.json()["disabled"] is True
    assert anon_client.get("/auth/me", headers=setup["bob"]).status_code == 401


def test_duplicates_and_unknown_ids(anon_client, setup):
    a = setup["admin"]
    assert anon_client.post("/admin/collections", json={"name": "rh"}, headers=a).status_code == 409
    assert (
        anon_client.post("/admin/groups", json={"name": "lecteurs-tech"}, headers=a).status_code
        == 409
    )
    assert anon_client.put("/admin/groups/999/members/1", headers=a).status_code == 404
    grant = anon_client.put(
        f"/admin/groups/{setup['group']['id']}/collections/999",
        json={"permission": "read"},
        headers=a,
    )
    assert grant.status_code == 404


# --- Postgres lui-même, sous le rôle rag_api (comme le ferait une injection SQL) ---------


def api_query(db_url: str, headers: dict[str, str] | None, statement: str, params=()):
    """Requête directe sous rag_api, avec (ou sans) l'empreinte d'un jeton en contexte."""
    url = make_conninfo(db_url, user="rag_api", password=API_DB_PASSWORD)
    with psycopg.connect(url) as conn:
        if headers:
            token = headers["Authorization"].removeprefix("Bearer ")
            conn.execute(
                "SELECT set_config('app.token_hash', %s, true)",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
        try:
            return conn.execute(statement, params).fetchall()
        finally:
            conn.rollback()


def test_rls_filters_chunks_in_postgres(setup, sql, db_url):
    # Un chunk dans chaque collection (écrits par le propriétaire, comme le worker)
    for doc in (setup["doc_rh"], setup["doc_tech"]):
        sql(
            "INSERT INTO chunks (document_id, collection_id, chunk_index, page, content,"
            " embedding, embed_model) SELECT id, collection_id, 0, 1, 'texte', %s::vector,"
            " 'bge-m3' FROM documents WHERE id = %s",
            (VECTOR, doc["id"]),
        )
    count = "SELECT count(*) FROM chunks"
    assert api_query(db_url, setup["admin"], count) == [(2,)]
    assert api_query(db_url, setup["bob"], count) == [(1,)]
    assert api_query(db_url, setup["carol"], count) == [(0,)]
    assert api_query(db_url, None, count) == [(0,)]  # sans jeton : rien


def test_rag_api_cannot_read_token_hashes(setup, db_url):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api_query(db_url, setup["admin"], "SELECT token_hash FROM tokens")


def test_forged_context_gives_nothing(setup, db_url):
    # Une injection SQL ne peut pas se faire passer pour un autre : il lui faudrait son jeton
    rows = api_query(
        db_url,
        None,
        "SELECT set_config('app.token_hash', 'nimporte-quoi', true), count(*) FROM documents",
    )
    assert rows[0][1] == 0


def test_non_admin_cannot_write_governance_tables_in_postgres(setup, db_url):
    for statement in (
        "INSERT INTO groups (name) VALUES ('pirates')",
        "UPDATE users SET is_admin = true",
        "INSERT INTO collection_grants SELECT g.id, c.id, 'write' FROM groups g, collections c",
    ):
        try:
            rows = api_query(db_url, setup["bob"], statement + " RETURNING 1")
        except psycopg.errors.InsufficientPrivilege:
            continue
        assert rows == []  # UPDATE filtré par la RLS : aucune ligne touchée


def test_api_refuses_to_start_as_owner(anon_client, run_db):
    with pytest.raises(RuntimeError, match="contourne la Row-Level Security"):
        run_db(check_api_connection)
