"""Smoke tests — no network/login (TestClient without lifespan)."""

from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_v1_info():
    assert client.get("/v1").json()["service"] == "OpenWebUI Proxy"


def test_unknown_route_returns_friendly_404():
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


def test_auth_status_shape_without_login():
    # Lifespan is not run, so no sign-in happened.
    body = client.get("/auth/status").json()
    assert body["authenticated"] is False
    assert "upstream" in body
