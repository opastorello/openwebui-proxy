# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OpenWebUI Proxy** is a local **FastAPI** server that exposes an OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) in front of an [Open WebUI](https://openwebui.com) instance. Open WebUI already speaks OpenAI at `/api/chat/completions`, but it lives under `/api` and requires a logged-in **JWT** (when API keys are disabled). This proxy signs in with an Open WebUI account, keeps the JWT fresh, and exposes a clean `/v1` endpoint. It is a **transparent passthrough** — the upstream responses are already OpenAI-shaped, so the proxy mostly rewrites the path and injects auth.

The whole app is a single file: **`app.py`**. Interactive docs at **`/docs`** (`/redoc` disabled).

## Running

```bash
docker compose up                          # reads .env for UPSTREAM_BASE + OWUI_*
uvicorn app:app --port 5002                # local; app.py loads ./.env itself
```

`PYTHONUNBUFFERED=1` is set in the image so logs/sign-in appear immediately.

## Tooling / CI

`ruff` (lint + format, `ruff.toml`) and `pytest` smoke tests in `tests/` — they use `TestClient` **without** the lifespan, so no sign-in/network happens. Dev deps in `requirements-dev.txt`. Run locally: `pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q`. CI (`.github/workflows/ci.yml`) runs lint + tests (3.11/3.12) + a Docker build; `docker-publish.yml` pushes a multi-arch image to GHCR on `main` and `v*` tags.

## Architecture (`app.py`)

- **`_load_dotenv()`** — tiny dependency-free `.env` loader run at import (so local `uvicorn` picks up secrets without `python-dotenv`). Docker passes env directly, so `setdefault` doesn't override it.
- **`TokenManager`** — `get_token()` returns a valid JWT: if `OWUI_TOKEN` is set it's used as-is; otherwise it POSTs `OWUI_EMAIL`/`OWUI_PASSWORD` to `/api/v1/auths/signin`, caches the JWT, decodes its `exp`, and re-signs-in 60s before expiry. Thread-safe via `threading.Lock()`.
- **Routes** (sync `def`, blocking `urllib` runs in Starlette's threadpool):
  - `POST /v1/chat/completions` (+ `/chat/completions`) — injects `DEFAULT_MODEL` if the body omits `model`, forwards the body to `{UPSTREAM_BASE}/api/chat/completions` with `Bearer <jwt>`. Streaming uses a sync generator that pipes raw upstream bytes through `StreamingResponse` (the upstream SSE is already OpenAI `chat.completion.chunk` format). Non-streaming returns the upstream JSON bytes verbatim.
  - `GET /v1/models` (+ `/models`) — passthrough to `{UPSTREAM_BASE}/api/models` (already `{"data":[...]}`).
  - `GET /auth/status` — signed-in user, upstream, default model, JWT `expires_in_seconds`.
  - `GET /health`, `GET /v1` (info), `/` → redirect to `/docs`, and a friendly OpenAI-style 404 handler.
- **`require_auth`** — optional client gate: an `HTTPBearer` scheme (so `/docs` shows **Authorize**) that enforces `PROXY_API_KEY` when set; open otherwise.

### Invisibility

The proxy deliberately calls **only** `/api/chat/completions` with a minimal body (no `chat_id`/`session_id`/`background_tasks`) and never the chat-persistence endpoints (`/api/v1/chats/...`), so requests do not appear in the Open WebUI chat history. This was verified empirically (chat list count unchanged after proxy calls).

## Secrets

`.env` holds the Open WebUI login (and the real `UPSTREAM_BASE`) and is git-ignored. Committed files use generic placeholders (`http://localhost:3000`) — never put the real instance URL or credentials in tracked files.
