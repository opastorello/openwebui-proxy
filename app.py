"""
OpenWebUI Proxy — a clean OpenAI-compatible /v1 in front of an Open WebUI instance.

Open WebUI already speaks OpenAI at `/api/chat/completions`, but (a) it lives under
`/api` instead of `/v1` and (b) it needs a logged-in JWT (no API key when API keys
are disabled). This proxy signs in with your Open WebUI account, keeps the JWT fresh,
and exposes a standard `/v1` endpoint that any OpenAI client can use.

Interactive docs (Swagger): http://localhost:5002/docs
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field


# ─── tiny .env loader (no dependency) ────────────────────────────────
def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


# ─── Config (env) ────────────────────────────────────────────────────
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://localhost:3000").rstrip("/")
OWUI_EMAIL = os.getenv("OWUI_EMAIL", "")
OWUI_PASSWORD = os.getenv("OWUI_PASSWORD", "")
OWUI_TOKEN = os.getenv("OWUI_TOKEN", "")  # optional: use a static JWT instead of login
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "")
PROXY_PORT = int(os.getenv("PROXY_PORT", "5002"))
BACKEND_TIMEOUT = float(os.getenv("BACKEND_TIMEOUT", "600"))

SIGNIN_PATH = "/api/v1/auths/signin"
CHAT_PATH = "/api/chat/completions"
MODELS_PATH = "/api/models"


def _decode_jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
    except Exception:
        return 0


# ─── Token manager: sign in with email/password, refresh before expiry ─
class TokenManager:
    def __init__(self) -> None:
        self._token = ""
        self._exp = 0
        self._user = ""
        self._lock = threading.Lock()

    def _signin(self) -> None:
        if not OWUI_EMAIL or not OWUI_PASSWORD:
            raise RuntimeError("OWUI_EMAIL / OWUI_PASSWORD not set (and no OWUI_TOKEN)")
        data = json.dumps({"email": OWUI_EMAIL, "password": OWUI_PASSWORD}).encode()
        req = urllib.request.Request(
            f"{UPSTREAM_BASE}{SIGNIN_PATH}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        self._token = body["token"]
        self._user = body.get("name", "")
        self._exp = _decode_jwt_exp(self._token)
        print(f"[AUTH] Signed in as {self._user} (token valid for {int(self._exp - time.time())}s)")

    def get_token(self) -> str:
        if OWUI_TOKEN:
            self._token, self._exp = OWUI_TOKEN, _decode_jwt_exp(OWUI_TOKEN)
            return OWUI_TOKEN
        with self._lock:
            if not self._token or time.time() > (self._exp - 60):
                self._signin()
            return self._token

    def status(self) -> dict:
        exp = self._exp or _decode_jwt_exp(self._token)
        return {
            "authenticated": bool(self._token),
            "user": self._user or None,
            "upstream": UPSTREAM_BASE,
            "default_model": DEFAULT_MODEL,
            "expires_in_seconds": max(0, int(exp - time.time())) if exp else None,
        }


tokens = TokenManager()


def _upstream_headers() -> dict:
    return {"Authorization": f"Bearer {tokens.get_token()}", "Content-Type": "application/json"}


# ─── Client auth (optional) ──────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False, description="Proxy API key (PROXY_API_KEY)")


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    if not PROXY_API_KEY:
        return
    token = credentials.credentials if credentials else ""
    if token != PROXY_API_KEY:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid proxy API key", "type": "auth_error"}},
        )


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: Optional[str] = Field(default=None, examples=["llama3.2"])
    messages: list
    stream: bool = False


# ─── App ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        tokens.get_token()
    except Exception as e:  # don't crash startup; report on first request instead
        print(f"[AUTH] initial sign-in failed: {e}")
    print(f"[PROXY] ready. Upstream={UPSTREAM_BASE} Default model={DEFAULT_MODEL} | docs: /docs")
    yield


app = FastAPI(
    title="OpenWebUI Proxy",
    description="OpenAI-compatible /v1 in front of an Open WebUI instance (auto-login, JWT refresh).",
    version="0.0.1",
    lifespan=lifespan,
    redoc_url=None,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/v1", include_in_schema=False)
@app.get("/v1/", include_in_schema=False)
def v1_root():
    return {
        "service": "OpenWebUI Proxy",
        "docs": "/docs",
        "endpoints": ["/v1/chat/completions", "/v1/models", "/health", "/auth/status"],
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/auth/status", tags=["meta"])
def auth_status(_: None = Depends(require_auth)):
    return tokens.status()


@app.get("/v1/models", tags=["openai"])
@app.get("/models", include_in_schema=False)
def models(_: None = Depends(require_auth)):
    req = urllib.request.Request(
        f"{UPSTREAM_BASE}{MODELS_PATH}", headers=_upstream_headers(), method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=BACKEND_TIMEOUT) as resp:
            return Response(content=resp.read(), media_type="application/json")
    except urllib.error.HTTPError as e:
        return Response(content=e.read(), status_code=e.code, media_type="application/json")
    except urllib.error.URLError as e:
        return JSONResponse(
            status_code=502, content={"error": {"message": f"Upstream failed: {e.reason}"}}
        )


@app.post("/v1/chat/completions", tags=["openai"])
@app.post("/chat/completions", include_in_schema=False)
def chat_completions(req: ChatCompletionRequest, _: None = Depends(require_auth)):
    body = req.model_dump(exclude_none=True)
    if not body.get("model") and DEFAULT_MODEL:
        body["model"] = DEFAULT_MODEL
    is_stream = bool(body.get("stream", False))

    ureq = urllib.request.Request(
        f"{UPSTREAM_BASE}{CHAT_PATH}",
        data=json.dumps(body).encode(),
        headers=_upstream_headers(),
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(ureq, timeout=BACKEND_TIMEOUT)
    except urllib.error.HTTPError as e:
        return Response(content=e.read(), status_code=e.code, media_type="application/json")
    except urllib.error.URLError as e:
        return JSONResponse(
            status_code=502, content={"error": {"message": f"Upstream failed: {e.reason}"}}
        )

    if is_stream:

        def gen():
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    try:
        data = resp.read()
    finally:
        resp.close()
    return Response(content=data, media_type="application/json")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "message": f"Unknown endpoint: {request.url.path} — see /docs",
                "type": "not_found",
            }
        },
    )
