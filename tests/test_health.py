from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_version(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc123")
    assert client.get("/health").json()["version"] == "abc123"
