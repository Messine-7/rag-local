"""Connexion : mot de passe, jetons de session, clés API (phase 5.1).

Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

from app.auth import create_user
from conftest import TEST_PASSWORD, TEST_USER


def login(c, username=TEST_USER, password=TEST_PASSWORD):
    return c.post("/auth/login", json={"username": username, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_login_and_me(anon_client, run_db, sql):
    run_db(lambda conn: create_user(conn, TEST_USER, TEST_PASSWORD))
    response = login(anon_client, username="  Alice ")  # espaces et majuscules ignorés
    assert response.status_code == 200
    token = response.json()["token"]
    assert token.startswith("rag_s_")
    assert response.json()["expires_at"] is not None
    me = anon_client.get("/auth/me", headers=bearer(token))
    assert me.json() == {"id": 1, "username": "alice", "is_admin": False}
    # Le jeton n'est jamais stocké en clair, ni le mot de passe
    assert sql("SELECT count(*) FROM tokens WHERE token_hash = %s", (token,)) == [(0,)]
    assert sql("SELECT password_hash LIKE '$argon2id$%%' FROM users") == [(True,)]


def test_wrong_password_and_unknown_user_look_the_same(anon_client, run_db):
    run_db(lambda conn: create_user(conn, TEST_USER, TEST_PASSWORD))
    wrong = login(anon_client, password="pas-le-bon-mot")
    unknown = login(anon_client, username="personne")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_missing_or_bad_token(anon_client):
    no_token = anon_client.get("/auth/me")
    assert no_token.status_code == 401
    assert no_token.headers["WWW-Authenticate"] == "Bearer"
    assert anon_client.get("/auth/me", headers=bearer("rag_s_invente")).status_code == 401


def test_health_stays_public(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_logout_revokes_the_session(client):
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_expired_session_is_refused(client, sql):
    sql("UPDATE tokens SET expires_at = now() - interval '1 second'")
    assert client.get("/auth/me").status_code == 401


def test_disabled_user_is_refused(client, sql):
    sql("UPDATE users SET disabled_at = now()")
    assert client.get("/auth/me").status_code == 401
    assert login(client).status_code == 401


def test_api_key_lifecycle(client):
    created = client.post("/auth/api-keys", json={"name": "laptop"})
    assert created.status_code == 201
    key = created.json()
    assert key["token"].startswith("rag_k_")
    assert key["expires_at"] is None

    # La clé suffit, sans session
    assert client.get("/auth/me", headers=bearer(key["token"])).json()["username"] == "alice"

    listed = client.get("/auth/api-keys").json()
    assert [(k["id"], k["name"], k["prefix"]) for k in listed] == [
        (key["id"], "laptop", key["token"][:12])
    ]
    assert "token" not in listed[0]  # la valeur n'est plus jamais montrée

    assert client.delete(f"/auth/api-keys/{key['id']}").status_code == 204
    assert client.get("/auth/me", headers=bearer(key["token"])).status_code == 401
    assert client.delete(f"/auth/api-keys/{key['id']}").status_code == 404


def test_cannot_revoke_someone_elses_key(client, anon_client, run_db):
    key_id = client.post("/auth/api-keys", json={"name": "à alice"}).json()["id"]
    run_db(lambda conn: create_user(conn, "bob", "mot-de-passe-bob"))
    bob = bearer(login(anon_client, "bob", "mot-de-passe-bob").json()["token"])
    assert anon_client.delete(f"/auth/api-keys/{key_id}", headers=bob).status_code == 404


def test_user_without_password_cannot_log_in(anon_client, run_db):
    run_db(lambda conn: create_user(conn, "robot", None))
    assert login(anon_client, "robot", "nimporte-quoi").status_code == 401
