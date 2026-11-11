"""Report what VIGIL needs from a FastAPI app: a few lines, no agent.

    from fastapi import FastAPI
    from vigil_instrument import Vigil, instrument_fastapi

    app = FastAPI()
    vigil = instrument_fastapi(app, Vigil(version="v3", targets={"auth": "auth", "pdf-render": "pdf-render"}))
    lookups = vigil.customer("result_requests_total", duration="result_request_duration_seconds")
    vigil.count_route("GET", "/result/{roll}", lookups)

See instrument/README.md for outbound calls and the full metric contract.
"""

from .core import Attempt, Call, CustomerAction, Vigil

__all__ = ["Vigil", "CustomerAction", "Call", "Attempt", "instrument_fastapi",
           "VigilTransport", "AsyncVigilTransport", "requests_adapter"]
__version__ = "0.1.0"


def __getattr__(name: str):
    # Load the adapters only when used, so importing the core never requires FastAPI or httpx.
    if name == "instrument_fastapi":
        from .asgi import instrument_fastapi
        return instrument_fastapi
    if name in ("VigilTransport", "AsyncVigilTransport", "requests_adapter"):
        from . import clients
        return getattr(clients, name)
    raise AttributeError(name)
