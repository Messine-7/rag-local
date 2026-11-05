import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings, load_settings
from app.llm import Generation, LLMError, get_llm
from app.main import app

TEST_SETTINGS = Settings(
    llm_base_url="http://serveur-de-test/v1",
    llm_api_key="cle",
    llm_model="modele-a",
    llm_allowed_models=("modele-a", "modele-b"),
    embed_model="embed-test",
    llm_timeout_s=1.0,
)


class FakeLLM:
    """Remplace le serveur de modèles : la CI n'a pas de GPU."""

    def __init__(self) -> None:
        self.fail = False
        self.calls: list[dict] = []

    async def generate(self, prompt, *, model, max_tokens, temperature):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens})
        if self.fail:
            raise LLMError("connexion refusée")
        return Generation(text=f"réponse de {model}", prompt_tokens=4, completion_tokens=6)

    async def embed(self, texts, *, model):
        if self.fail:
            raise LLMError("connexion refusée")
        return [[0.1, 0.2, 0.3] for _ in texts]


client = TestClient(app)


@pytest.fixture
def fake_llm():
    fake = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: fake
    app.dependency_overrides[get_settings] = lambda: TEST_SETTINGS
    yield fake
    app.dependency_overrides.clear()


def test_generate_uses_default_model(fake_llm):
    response = client.post("/llm/generate", json={"prompt": "Bonjour"})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "modele-a"
    assert body["answer"] == "réponse de modele-a"
    assert body["completion_tokens"] == 6
    assert body["latency_ms"] >= 0
    assert fake_llm.calls[0]["max_tokens"] == 256


def test_generate_accepts_allowed_model(fake_llm):
    response = client.post("/llm/generate", json={"prompt": "Bonjour", "model": "modele-b"})
    assert response.status_code == 200
    assert response.json()["model"] == "modele-b"


def test_generate_refuses_unknown_model(fake_llm):
    response = client.post("/llm/generate", json={"prompt": "Bonjour", "model": "inconnu:1b"})
    assert response.status_code == 400
    assert fake_llm.calls == []


def test_generate_rejects_empty_prompt(fake_llm):
    assert client.post("/llm/generate", json={"prompt": ""}).status_code == 422


def test_generate_reports_unavailable_llm(fake_llm):
    fake_llm.fail = True
    response = client.post("/llm/generate", json={"prompt": "Bonjour"})
    assert response.status_code == 503
    assert "connexion refusée" in response.json()["detail"]


def test_embed(fake_llm):
    response = client.post("/llm/embed", json={"texts": ["un", "deux"]})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "embed-test"
    assert body["dimensions"] == 3
    assert len(body["embeddings"]) == 2


def test_settings_defaults():
    settings = load_settings({})
    assert settings.llm_model == "mistral-small3.2:24b"
    assert settings.llm_allowed_models == ("mistral-small3.2:24b", "gemma4:12b")
    assert settings.embed_model == "bge-m3"
    assert settings.llm_base_url == "http://ollama:11434/v1"


def test_settings_default_model_always_allowed():
    settings = load_settings({"LLM_MODEL": "autre:7b", "LLM_ALLOWED_MODELS": "a, b"})
    assert settings.llm_allowed_models == ("autre:7b", "a", "b")
