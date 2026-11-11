"""Record calls to other services made with httpx or requests.

Only hosts listed in Vigil(targets=...) are recorded, under their vigil.yaml
service name. Outcome, the same rule the demo services use:
    a response below 500          -> ok      (a 404 is an answer, not a broken service)
    a 5xx response                -> error
    no response: timed out        -> timeout
    no response: refused, DNS ... -> error
"""

from __future__ import annotations

import time
from typing import Any

from .core import Vigil, _is_timeout


def _outcome(status: int) -> str:
    return "error" if status >= 500 else "ok"


class VigilTransport:
    """httpx (sync): httpx.Client(transport=VigilTransport(vigil))."""

    def __init__(self, vigil: Vigil, transport: Any = None):
        import httpx
        self.vigil = vigil
        self.inner = transport or httpx.HTTPTransport()

    def handle_request(self, request: Any) -> Any:
        target = self.vigil.target_for(request.url.host, request.url.port)
        if target is None:
            return self.inner.handle_request(request)
        start = time.perf_counter()
        try:
            response = self.inner.handle_request(request)
        except Exception as exc:
            self.vigil.observe_call(target, "timeout" if _is_timeout(exc) else "error", time.perf_counter() - start)
            raise
        self.vigil.observe_call(target, _outcome(response.status_code), time.perf_counter() - start)
        return response

    def close(self) -> None:
        self.inner.close()

    def __enter__(self) -> "VigilTransport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncVigilTransport:
    """httpx (async): httpx.AsyncClient(transport=AsyncVigilTransport(vigil))."""

    def __init__(self, vigil: Vigil, transport: Any = None):
        import httpx
        self.vigil = vigil
        self.inner = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: Any) -> Any:
        target = self.vigil.target_for(request.url.host, request.url.port)
        if target is None:
            return await self.inner.handle_async_request(request)
        start = time.perf_counter()
        try:
            response = await self.inner.handle_async_request(request)
        except Exception as exc:
            self.vigil.observe_call(target, "timeout" if _is_timeout(exc) else "error", time.perf_counter() - start)
            raise
        self.vigil.observe_call(target, _outcome(response.status_code), time.perf_counter() - start)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def __aenter__(self) -> "AsyncVigilTransport":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def _make_transports() -> None:
    """Make the two transports real httpx transports (when httpx is installed), so
    httpx accepts them anywhere it accepts its own."""
    try:
        import httpx
    except ImportError:
        return
    global VigilTransport, AsyncVigilTransport
    VigilTransport = type("VigilTransport", (VigilTransport, httpx.BaseTransport), {})
    AsyncVigilTransport = type("AsyncVigilTransport", (AsyncVigilTransport, httpx.AsyncBaseTransport), {})


_make_transports()


def requests_adapter(vigil: Vigil, **adapter_kwargs: Any) -> Any:
    """requests: session.mount("http://", requests_adapter(vigil)) (and "https://")."""
    from urllib.parse import urlsplit

    from requests.adapters import HTTPAdapter

    class VigilAdapter(HTTPAdapter):
        def send(self, request: Any, **kwargs: Any) -> Any:
            url = urlsplit(request.url)
            target = vigil.target_for(url.hostname, url.port)
            if target is None:
                return super().send(request, **kwargs)
            start = time.perf_counter()
            try:
                response = super().send(request, **kwargs)
            except Exception as exc:
                vigil.observe_call(target, "timeout" if _is_timeout(exc) else "error", time.perf_counter() - start)
                raise
            vigil.observe_call(target, _outcome(response.status_code), time.perf_counter() - start)
            return response

    return VigilAdapter(**adapter_kwargs)
