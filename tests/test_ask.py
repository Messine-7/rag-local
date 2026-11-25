"""Questions (POST /ask) et conversations (phase 5.3).

Le modèle est remplacé par un faux (la CI n'a ni GPU ni Ollama) ; la base est réelle, avec
la Row-Level Security : chacun ne reçoit que des passages qu'il a le droit de lire.
Base de test : voir conftest.py (ignorés sans TEST_DATABASE_URL).
"""

import json

import pytest

from app.auth import create_user
from app.conversations import NO_PASSAGE_ANSWER
from app.llm import LLMError, Usage, get_llm
from app.main import app
from conftest import login_headers

VECTOR = [0.0] * 1023 + [1.0]
VECTOR_SQL = "[" + ",".join(str(x) for x in VECTOR) + "]"


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    async def embed(self, texts, *, model):
        return [VECTOR for _ in texts]

    async def stream_chat(self, messages, *, model, max_tokens, temperature, reasoning_effort):
        self.calls.append(
            {"messages": messages, "model": model, "reasoning_effort": reasoning_effort}
        )
        if self.fail:
            raise LLMError("Ollama injoignable")
        for part in ("D'après ", "le document ", "[1]."):
            yield part
        yield Usage(prompt_tokens=120, completion_tokens=8)


@pytest.fixture
def fake_llm():
    fake = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_llm, None)


@pytest.fixture
def world(anon_client, admin_headers, sql, run_db):
    """Collections rh et tech, un document et un chunk dans chacune (même vecteur, mots
    différents). bob lit tech ; carol n'a accès à rien."""
    for name in ("bob", "carol"):
        run_db(lambda conn, n=name: create_user(conn, n, f"mot-de-passe-{n}"))
    sql(
        """
        WITH c AS (INSERT INTO collections (name) VALUES ('rh'), ('tech') RETURNING id, name),
             g AS (INSERT INTO groups (name) VALUES ('lecteurs-tech') RETURNING id),
             m AS (INSERT INTO group_members SELECT g.id, u.id FROM g, users u
                   WHERE u.username = 'bob')
        INSERT INTO collection_grants SELECT g.id, c.id, 'read' FROM g, c WHERE c.name = 'tech'
        """
    )
    for collection, filename, content in (
        ("rh", "salaires.pdf", "La grille des salaires 2026 augmente de trois pour cent."),
        ("tech", "architecture.pdf", "Le serveur GPU héberge Ollama et la base Postgres."),
    ):
        sql(
            """
            WITH d AS (
                INSERT INTO documents (collection_id, filename, sha256, size_bytes, status)
                SELECT id, %s, md5(%s), 1, 'ready' FROM collections WHERE name = %s
                RETURNING id, collection_id)
            INSERT INTO chunks (document_id, collection_id, chunk_index, page, content,
                                embedding, embed_model)
            SELECT id, collection_id, 0, 2, %s, %s::vector, 'bge-m3' FROM d
            """,
            (filename, filename, collection, content, VECTOR_SQL),
        )
    return {
        "admin": admin_headers,
        "bob": login_headers(anon_client, "bob", "mot-de-passe-bob"),
        "carol": login_headers(anon_client, "carol", "mot-de-passe-carol"),
    }


def ask(c, headers, question, **extra):
    body = {"question": question, "stream": False, **extra}
    return c.post("/ask", json=body, headers=headers)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


# --- Réponses et droits -----------------------------------------------------------------


def test_reader_gets_only_passages_he_can_read(anon_client, world, fake_llm):
    response = ask(anon_client, world["bob"], "Où tourne Ollama ?")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] == "D'après le document [1]."
    assert [c["filename"] for c in body["citations"]] == ["architecture.pdf"]
    assert body["citations"][0]["page"] == 2
    assert body["completion_tokens"] == 8
    # Pas de raisonnement préalable par défaut (sinon gemma4 épuise max_tokens en réflexion)
    assert fake_llm.calls[0]["reasoning_effort"] == "none"
    # Le prompt envoyé au modèle ne contient que l'extrait autorisé
    system = fake_llm.calls[0]["messages"][0]["content"]
    assert "serveur GPU" in system
    assert "salaires" not in system


def test_admin_gets_all_and_full_text_ranks_exact_words_first(anon_client, world, fake_llm):
    # Même vecteur pour les deux chunks : c'est la recherche plein texte qui départage
    body = ask(anon_client, world["admin"], "salaires 2026").json()
    assert [c["filename"] for c in body["citations"]] == ["salaires.pdf", "architecture.pdf"]


def test_collections_filter(anon_client, world, fake_llm):
    body = ask(anon_client, world["admin"], "serveur", collections=["rh"]).json()
    assert [c["collection"] for c in body["citations"]] == ["rh"]


def test_no_accessible_passage_means_no_model_call(anon_client, world, fake_llm):
    body = ask(anon_client, world["carol"], "Quels sont les salaires ?").json()
    assert body["answer"] == NO_PASSAGE_ANSWER
    assert body["citations"] == []
    assert fake_llm.calls == []


def test_unknown_model_refused(anon_client, world, fake_llm):
    assert ask(anon_client, world["bob"], "x", model="inconnu:1b").status_code == 400


def test_model_failure(anon_client, world, fake_llm):
    fake_llm.fail = True
    assert ask(anon_client, world["bob"], "Ollama ?").status_code == 503


# --- Streaming (SSE) --------------------------------------------------------------------


def test_streaming_events(anon_client, world, fake_llm):
    response = anon_client.post("/ask", json={"question": "Ollama ?"}, headers=world["bob"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [e for e, _ in events] == ["meta", "token", "token", "token", "done"]
    meta, done = events[0][1], events[-1][1]
    assert meta["citations"][0]["filename"] == "architecture.pdf"
    assert "".join(d["text"] for e, d in events if e == "token") == "D'après le document [1]."
    assert done["completion_tokens"] == 8
    # La réponse est enregistrée à la fin du flux
    detail = anon_client.get(
        f"/conversations/{meta['conversation_id']}", headers=world["bob"]
    ).json()
    assert [m["role"] for m in detail["history"]] == ["user", "assistant"]
    assert detail["history"][1]["id"] == done["message_id"]


def test_streaming_error_event(anon_client, world, fake_llm):
    fake_llm.fail = True
    response = anon_client.post("/ask", json={"question": "Ollama ?"}, headers=world["bob"])
    assert [e for e, _ in parse_sse(response.text)] == ["meta", "error"]


# --- Conversations ----------------------------------------------------------------------


def test_follow_up_sends_history(anon_client, world, fake_llm):
    first = ask(anon_client, world["bob"], "Où tourne Ollama ?").json()
    second = ask(
        anon_client, world["bob"], "Et la base ?", conversation_id=first["conversation_id"]
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    sent = fake_llm.calls[1]["messages"]
    assert [(m["role"], m["content"]) for m in sent[1:]] == [
        ("user", "Où tourne Ollama ?"),
        ("assistant", "D'après le document [1]."),
        ("user", "Et la base ?"),
    ]
    listed = anon_client.get("/conversations", headers=world["bob"]).json()
    assert [(c["title"], c["messages"]) for c in listed] == [("Où tourne Ollama ?", 4)]


def test_conversations_are_private_even_from_admins(anon_client, world, fake_llm):
    conversation_id = ask(anon_client, world["bob"], "Ollama ?").json()["conversation_id"]
    for who in ("admin", "carol"):
        headers = world[who]
        assert anon_client.get("/conversations", headers=headers).json() == []
        assert (
            anon_client.get(f"/conversations/{conversation_id}", headers=headers).status_code == 404
        )
        # Ni la lire ni y écrire
        hijack = ask(anon_client, headers, "x", conversation_id=conversation_id)
        assert hijack.status_code == 404


def test_delete_conversation(anon_client, world, fake_llm, sql):
    conversation_id = ask(anon_client, world["bob"], "Ollama ?").json()["conversation_id"]
    path = f"/conversations/{conversation_id}"
    assert anon_client.delete(path, headers=world["carol"]).status_code == 404
    assert anon_client.delete(path, headers=world["bob"]).status_code == 204
    assert sql("SELECT count(*) FROM messages") == [(0,)]
