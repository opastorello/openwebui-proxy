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


def test_wrap_reasoning_final_moves_to_think_tags():
    obj = {
        "choices": [
            {"message": {"role": "assistant", "content": "Hi", "reasoning_content": "thinking"}}
        ]
    }
    msg = app._wrap_reasoning_final(obj)["choices"][0]["message"]
    assert "reasoning_content" not in msg
    assert msg["content"] == "<think>\nthinking\n</think>\n\nHi"


def test_transform_chunk_opens_and_closes_think():
    state = {"open": False}
    a = {"choices": [{"delta": {"reasoning_content": "ponder"}}]}
    app._transform_chunk(a, state)
    assert a["choices"][0]["delta"]["content"] == "<think>\nponder" and state["open"] is True
    b = {"choices": [{"delta": {"content": "answer"}}]}
    app._transform_chunk(b, state)
    assert b["choices"][0]["delta"]["content"] == "\n</think>\n\nanswer" and state["open"] is False
