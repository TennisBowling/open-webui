"""Request-shim for headless (request-free) chat generation.

The chat pipeline (``process_chat_payload`` → ``chat_completion_handler`` →
``process_chat_response``) was written to take a FastAPI ``Request``. In
practice it only ever touches three things on it:

  * ``request.app``     — the FastAPI app singleton (→ ``app.state`` config,
                          MODELS, redis, oauth managers, …)
  * ``request.state``   — a per-request mutable namespace (``direct``,
                          ``model``, ``metadata``, ``token``, ``cached_queries``,
                          ``subagent_id_by_tool_call``, …)
  * ``request.cookies`` — read only for ``oauth_session_id``

When the server drives a generation with no inbound HTTP request (the
autonomous message-queue drain), there is no ambient ``Request`` to pass. This
shim provides an object that satisfies exactly those three attributes so the
existing pipeline runs unchanged.

It deliberately does NOT subclass ``starlette.requests.Request`` — that class
requires an ASGI ``scope`` and raises on most attribute access without a live
connection. We only need a duck-typed carrier.
"""

from __future__ import annotations

from typing import Any, Optional

from starlette.datastructures import Headers, State


class HeadlessRequest:
    """Minimal duck-typed stand-in for ``fastapi.Request`` for headless runs.

    ``app`` is the real FastAPI app (so ``app.state`` singletons are shared with
    live requests). ``state`` is a fresh per-run namespace. ``cookies`` carries
    only what the pipeline reads (``oauth_session_id``), defaulting to empty.

    ``headers``/``method`` are NOT read by today's generation hot path (verified:
    it only touches ``app``/``state``/``cookies``), but are provided as inert
    defaults so a future code path that incidentally reads them degrades to an
    empty header / ``POST`` instead of raising ``AttributeError`` mid-generation
    — which on the headless drain would silently wedge the queue rather than
    surface a clear error.
    """

    def __init__(
        self,
        app: Any,
        *,
        cookies: Optional[dict] = None,
        token: Any = None,
    ) -> None:
        self.app = app
        self.state = State()
        self.cookies: dict = dict(cookies or {})
        self.headers = Headers({})
        self.method = "POST"
        # The HTTP middleware (`check_url`) normally stamps request.state.token
        # from the Authorization header; tool-server bearer auth reads it. For
        # headless runs there is no header, so default to None (no bearer).
        self.state.token = token
