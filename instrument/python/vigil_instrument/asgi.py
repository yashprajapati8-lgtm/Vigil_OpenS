"""FastAPI (and the Starlette it is built on): one ASGI middleware, plus the /metrics route."""

from __future__ import annotations

import time
from typing import Any

from .core import UNMATCHED, Vigil


def _template(scope: dict, app: Any) -> str:
    """The route template that served this request, e.g. "/result/{roll}"."""
    route = scope.get("route")                       # FastAPI puts the matched route here
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    if path:
        return path
    # Plain Starlette: ask the router which route matches.
    try:
        from starlette.routing import Match
        router = getattr(app, "router", None)
        for r in getattr(router, "routes", []):
            match, _ = r.matches(scope)
            if match == Match.FULL:
                return getattr(r, "path_format", None) or getattr(r, "path", UNMATCHED)
    except Exception:
        pass
    return UNMATCHED


class VigilMiddleware:
    """Records every HTTP request: route template, method, status, duration.
    An exception that escapes the app is recorded as 500 and re-raised."""

    def __init__(self, app: Any, vigil: Vigil, root_app: Any = None):
        self.app = app
        self.vigil = vigil
        self.root_app = root_app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in self.vigil.skip:
            await self.app(scope, receive, send)
            return
        status = {"code": 500}
        start = time.perf_counter()

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.vigil.observe_request(_template(scope, self.root_app or scope.get("app")), scope.get("method", "GET"),
                                       status["code"], time.perf_counter() - start)


def instrument_fastapi(app: Any, vigil: Vigil | None = None, *, metrics_path: str = "/metrics") -> Vigil:
    """Add VIGIL's metrics to a FastAPI or Starlette app and serve them at /metrics.

        app = FastAPI()
        vigil = instrument_fastapi(app, Vigil(version="v3", targets={"auth": "auth"}))
    """
    from starlette.responses import Response

    vigil = vigil or Vigil()
    app.add_middleware(VigilMiddleware, vigil=vigil, root_app=app)

    async def metrics(_request: Any) -> Response:
        body, ctype = vigil.exposition()
        return Response(body, media_type=ctype)

    app.add_route(metrics_path, metrics, methods=["GET"], include_in_schema=False)
    vigil.skip.add(metrics_path)
    return vigil
