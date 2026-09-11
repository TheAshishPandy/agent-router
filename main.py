"""
Agent Router — FastAPI entrypoint.

Mounts the proxy router (proxy.py) at /api, exposes /health, serves shared
static assets, and runs background tasks (session cleanup, upstream health).
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from platform_config import cfg, service_port, generate_request_id
from proxy import router as proxy_router

PORT = service_port("proxy")

# CORS allowlist: localhost on the proxy port + extras from data/tailscale.json.
ALLOWED_ORIGINS = [
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
]

_DATA_DIR = os.environ.get("AGENT_ROUTER_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data"
)
_tailscale_config = os.path.join(_DATA_DIR, "tailscale.json")
if os.path.isfile(_tailscale_config):
    try:
        with open(_tailscale_config) as _f:
            _ts = json.load(_f)
        for addr in _ts.get("allowed_origins", []):
            if addr and addr not in ALLOWED_ORIGINS:
                ALLOWED_ORIGINS.append(addr)
    except Exception:
        pass


app = FastAPI(
    title="Agent Router",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


_ERROR_CODE_MAP = {
    400: "BAD_REQUEST",
    401: "AUTH_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "RATE_LIMIT_EXCEEDED",
    499: "CLIENT_DISCONNECTED",
    502: "UPSTREAM_UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE",
    504: "TIMEOUT",
}


@app.exception_handler(StarletteHTTPException)
async def unified_http_exception(request: Request, exc: StarletteHTTPException):
    req_id = getattr(request.state, "request_id", "") if hasattr(request, "state") else ""
    code = _ERROR_CODE_MAP.get(exc.status_code, "ERROR")
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    body = {"error": code, "message": message, "request_id": req_id}
    headers = {"X-Request-ID": req_id} if req_id else {}
    return JSONResponse(content=body, status_code=exc.status_code, headers=headers)


@app.exception_handler(RequestValidationError)
async def unified_validation_error(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", "") if hasattr(request, "state") else ""
    body = {
        "error": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "request_id": req_id,
        "details": exc.errors(),
    }
    headers = {"X-Request-ID": req_id} if req_id else {}
    return JSONResponse(content=body, status_code=422, headers=headers)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Inject X-Request-ID for correlation, strip server headers."""
    req_id = request.headers.get("x-request-id") or generate_request_id()
    request.state.request_id = req_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    for h in ("server", "x-powered-by"):
        if h in response.headers:
            del response.headers[h]
    return response


# Tailscale CGNAT range (100.64.0.0/10) for tailnet peer access.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"^http://100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proxy_router)

_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/api/static", StaticFiles(directory=_static_dir), name="shared-static")


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-router",
            "platform_version": cfg.get("_version", 0)}


@app.on_event("startup")
async def startup_tasks():
    """Start background maintenance tasks."""
    import asyncio
    from proxy import _cleanup_sessions_loop, _upstream_health_loop
    asyncio.create_task(_cleanup_sessions_loop())
    asyncio.create_task(_upstream_health_loop())


@app.on_event("shutdown")
async def graceful_shutdown():
    """Close persistent HTTP clients on shutdown."""
    import logging
    logger = logging.getLogger("agent-router")
    logger.info("[SHUTDOWN] closing HTTP clients")
    try:
        from proxy import _upstream_client, _minimax_client, _zai_client, _codex_client
        try:
            from provider_router import _clients as _pr_clients
            _extra = list(_pr_clients.values())
        except Exception:
            _extra = []
        for c in (_upstream_client, _minimax_client, _zai_client, _codex_client, *_extra):
            try:
                await c.aclose()
            except Exception:
                pass
    except Exception:
        pass
    logger.info("[SHUTDOWN] complete")


if __name__ == "__main__":
    import uvicorn
    host = cfg["services"]["proxy"]["host"]
    uvicorn.run("main:app", host=host, port=PORT, timeout_keep_alive=120)