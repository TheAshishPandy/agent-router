"""API Router — authenticated model access with privacy stripping.

Authenticated via API keys stored in ./data/proxy-keys.json.
Strips all privacy-sensitive headers before forwarding.
Supports SSE streaming for /v1/messages.
Tracks request source (Tailscale node identity) for the dashboard map.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import logging
import subprocess
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import httpx
from fastapi import APIRouter, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, RedirectResponse

from platform_config import cfg, upstream, timeout as cfg_timeout, generate_request_id
from provider_router import cascade_request, cascade_stream

logger = logging.getLogger("api-gateway")


_router_start_time = time.time()

def _humanize_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, s = divmod(seconds, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"

def _atomic_write_json(path: str, data, indent: int = 2, mode: int = 0):
    """Write JSON atomically: write to temp file then rename. Prevents corruption on crash."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp, path)
        if mode:
            os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


PROXY_TARGET = upstream("claude_api")
MINIMAX_TARGET = upstream("minimax")
ZAI_TARGET = cfg.get("upstream", {}).get("zai", "https://api.z.ai/api/anthropic")
CODEX_TARGET = cfg.get("upstream", {}).get("codex_api", "http://127.0.0.1:8006")
_DATA_DIR = os.environ.get("AGENT_ROUTER_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
KEYS_FILE = os.path.join(_DATA_DIR, "proxy-keys.json")
GEO_LOG_FILE = os.path.join(_DATA_DIR, "proxy-geo.json")
TAILSCALE_CONFIG = os.path.join(_DATA_DIR, "tailscale.json")

# ── Persistent HTTP clients (reuse connections, avoid per-request overhead) ──
_connect_timeout = cfg_timeout("connect")
_UPSTREAM_TIMEOUT = httpx.Timeout(timeout=cfg_timeout("claude_sonnet"), connect=_connect_timeout)
_upstream_client = httpx.AsyncClient(base_url=PROXY_TARGET, timeout=_UPSTREAM_TIMEOUT)
_minimax_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout=cfg_timeout("minimax"), connect=_connect_timeout))
_zai_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout=cfg.get("timeouts", {}).get("zai", 180.0), connect=_connect_timeout))
_codex_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout=cfg.get("timeouts", {}).get("codex", 240.0), connect=_connect_timeout))

# ── Upstream health tracking (P1-6) ──
_upstream_health = {
    "claude_api": {"status": "unknown", "last_check": 0, "consecutive_failures": 0, "last_error": ""},
    "minimax": {"status": "unknown", "last_check": 0, "consecutive_failures": 0, "last_error": ""},
    "zai": {"status": "unknown", "last_check": 0, "consecutive_failures": 0, "last_error": ""},
    "codex": {"status": "unknown", "last_check": 0, "consecutive_failures": 0, "last_error": ""},
}


async def _upstream_health_loop():
    """Periodically check upstream health and update status."""
    while True:
        await asyncio.sleep(30)
        # Check Claude API upstream
        try:
            resp = await _upstream_client.get("/health", timeout=5.0)
            _upstream_health["claude_api"].update({
                "status": "healthy" if resp.status_code == 200 else "degraded",
                "last_check": time.time(),
                "consecutive_failures": 0,
                "last_error": "",
            })
        except Exception as e:
            h = _upstream_health["claude_api"]
            h["consecutive_failures"] += 1
            h["last_check"] = time.time()
            h["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
            h["status"] = "down" if h["consecutive_failures"] >= 3 else "degraded"

        # Check MiniMax upstream
        try:
            resp = await _minimax_client.get(
                f"{MINIMAX_TARGET}/models", timeout=5.0,
                headers={"Authorization": f"Bearer {_MINIMAX_KEY[:20]}..."} if _MINIMAX_KEY else {}
            )
            _upstream_health["minimax"].update({
                "status": "healthy" if resp.status_code in (200, 401) else "degraded",
                "last_check": time.time(),
                "consecutive_failures": 0,
                "last_error": "",
            })
        except Exception as e:
            h = _upstream_health["minimax"]
            h["consecutive_failures"] += 1
            h["last_check"] = time.time()
            h["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
            h["status"] = "down" if h["consecutive_failures"] >= 3 else "degraded"


# ── Request lifecycle tracking ──
_request_log_lock = threading.Lock()
_active_requests: dict = {}   # req_id -> {endpoint, started, key_id, model, ...}
_error_log: deque = deque(maxlen=100)  # last 100 errors with full detail
_recent_requests: deque = deque(maxlen=50)  # last 50 completed requests
_request_counter = 0
USERS_FILE = os.path.join(_DATA_DIR, "dashboard-users.json")

# ── MiniMax Swarm (from platform config) ──
_SWARM_WORKERS = cfg.get("swarm", {}).get("workers", 3)
_swarm_enabled = cfg.get("swarm", {}).get("enabled", True)  # Global swarm toggle

def _is_swarm_enabled() -> bool:
    """Check if swarm is globally enabled."""
    return _swarm_enabled

def _set_swarm_enabled(enabled: bool):
    """Set global swarm enabled state."""
    global _swarm_enabled
    _swarm_enabled = enabled


def _parse_request_headers(request: Request) -> dict:
    """Parse per-request configuration headers.

    Returns dict with:
    - swarm_mode: 'parallel', 'deliberation', or 'none' (default: 'parallel')
    - swarm_workers: int (default from config, max 5)
    - temperature: float or None
    - max_tokens: int or None
    """
    headers = request.headers
    result = {
        "swarm_mode": "parallel",  # default
        "swarm_workers": _SWARM_WORKERS,
        "temperature": None,
        "max_tokens": None,
        "deliberation_rounds": 3,  # default rounds for deliberation mode
    }

    # X-Swarm-Mode: parallel|deliberation|none
    swarm_mode = headers.get("x-swarm-mode", "").lower()
    if swarm_mode in ("parallel", "deliberation", "none"):
        result["swarm_mode"] = swarm_mode

    # X-Deliberation-Rounds: override deliberation rounds (1-10)
    rounds_header = headers.get("x-deliberation-rounds")
    if rounds_header:
        try:
            rounds = int(rounds_header)
            result["deliberation_rounds"] = max(1, min(10, rounds))  # Clamp 1-10
        except ValueError:
            pass  # Use default

    # X-Swarm-Workers or X-Workers: override worker count
    workers_header = headers.get("x-swarm-workers") or headers.get("x-workers")
    if workers_header:
        try:
            workers = int(workers_header)
            # Clamp to 1-5 workers
            result["swarm_workers"] = max(1, min(5, workers))
        except ValueError:
            pass  # Use default

    # X-Temperature: override temperature
    temp_header = headers.get("x-temperature")
    if temp_header:
        try:
            result["temperature"] = float(temp_header)
        except ValueError:
            pass  # Use default from payload

    # X-Max-Tokens: override max_tokens
    max_tokens_header = headers.get("x-max-tokens")
    if max_tokens_header:
        try:
            result["max_tokens"] = int(max_tokens_header)
        except ValueError:
            pass  # Use default from payload

    return result


_swarm_stats = {
    "total_swarms": 0,
    "active_swarms": 0,
    "total_workers_run": 0,
    "workers_succeeded": 0,
    "worker_failures": 0,
    "consolidations": 0,
    "avg_worker_ms": 0,
    "avg_consolidation_ms": 0,
    "recent": deque(maxlen=20),
}
_swarm_lock = threading.Lock()

# ── Provider-level rate limit tracking ──
_provider_limits = {
    "minimax": {
        "rpm": 500, "tpm": 20_000_000,  # published limits
        "remaining_requests": None, "remaining_tokens": None,
        "reset_at": None, "last_updated": None,
    },
    "claude": {
        "plan": "Max", "window": "5-hour rolling + weekly cap",
        "note": "Limits are opaque — tracked locally only",
    },
}
_provider_lock = threading.Lock()


def _update_provider_limits(provider: str, resp_headers: dict):
    """Extract rate limit headers from upstream response."""
    with _provider_lock:
        if provider == "minimax":
            rl = _provider_limits["minimax"]
            remaining = resp_headers.get("x-ratelimit-remaining")
            if remaining is not None:
                try:
                    rl["remaining_requests"] = int(remaining)
                except (ValueError, TypeError):
                    pass
            # Some providers also send these
            for h in ("x-ratelimit-limit-requests", "x-ratelimit-limit"):
                v = resp_headers.get(h)
                if v:
                    try:
                        rl["rpm"] = int(v)
                    except (ValueError, TypeError):
                        pass
            reset = resp_headers.get("x-ratelimit-reset")
            if reset:
                rl["reset_at"] = reset
            rl["last_updated"] = datetime.now(timezone.utc).isoformat()

# ── Request Queue (serialize upstream Claude access) ──
_claude_semaphore = asyncio.Semaphore(2)  # Allow 2 concurrent Claude requests
_queue_depth = 0
_queue_lock = asyncio.Lock()
_total_queued = 0
_total_served = 0
QUEUE_WAIT_TIMEOUT = cfg_timeout("queue_wait")  # From platform.json
QUEUE_MAX_DEPTH = 20  # Maximum queue depth before rejecting (backpressure)
RETRY_DELAYS = [2, 8]       # Exponential backoff delays (seconds)
RETRYABLE_CODES = {429, 503}

# ── Dashboard Login / Session Auth ──

_sessions: dict = {}  # token → {"username": str, "created": str}
SESSION_EXPIRY = 86400  # 24 hours


def _hash_password(pw: str) -> str:
    """Hash password with bcrypt (cost=12)."""
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password(pw: str, stored_hash: str) -> bool:
    """Verify password against stored hash. Supports bcrypt and legacy SHA-256."""
    if stored_hash.startswith("$2"):
        # bcrypt hash
        return bcrypt.checkpw(pw.encode(), stored_hash.encode())
    # Legacy SHA-256 — verify and migrate
    if hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored_hash):
        return True
    return False


def _migrate_hash_if_needed(username: str, pw: str, stored_hash: str):
    """Upgrade legacy SHA-256 hash to bcrypt on successful login."""
    if not stored_hash.startswith("$2"):
        users = _load_users()
        if username in users:
            users[username]["password_hash"] = _hash_password(pw)
            _save_users(users)
            logger.info(f"Migrated password hash for '{username}' to bcrypt")


def _load_users() -> dict:
    if not os.path.isfile(USERS_FILE):
        return {}
    with open(USERS_FILE) as f:
        return json.load(f).get("users", {})


def _save_users(users: dict):
    _atomic_write_json(USERS_FILE, {"users": users}, mode=0o600)


def _create_default_users():
    """Create 3 default dashboard accounts on first run. Prints credentials to log."""
    if os.path.isfile(USERS_FILE):
        return
    passwords = {
        "sol": secrets.token_urlsafe(9),      # ~12 chars
        "pixel": secrets.token_urlsafe(9),
        "dev": secrets.token_urlsafe(9),
    }
    users = {}
    for username, pw in passwords.items():
        users[username] = {
            "password_hash": _hash_password(pw),
            "role": "admin" if username == "sol" else "viewer",
        }
    _save_users(users)
    # Print credentials so they can be noted
    logger.warning("=== Dashboard Login Credentials (first-run) ===")
    for username, pw in passwords.items():
        logger.warning(f"  {username}: {pw}")
    logger.warning("================================================")
    # Also print to stdout for LaunchAgent log capture
    print("=== Dashboard Login Credentials (first-run) ===")
    for username, pw in passwords.items():
        print(f"  {username}: {pw}")
    print("================================================")


def _validate_session(request: Request) -> Optional[str]:
    """Check session cookie. Returns username or None."""
    token = request.cookies.get("session")
    if not token or token not in _sessions:
        return None
    sess = _sessions[token]
    created = datetime.fromisoformat(sess["created"])
    if (datetime.now(timezone.utc) - created).total_seconds() > SESSION_EXPIRY:
        del _sessions[token]
        return None
    return sess["username"]


# ── Brute-force Protection ──
_login_attempts: dict = {}  # ip -> {"count": int, "first": float, "locked_until": float}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 900  # 15 minutes
_LOGIN_LOCKOUT = 900  # 15 min lockout after max attempts


def _check_login_rate(ip: str) -> Optional[str]:
    """Check if IP is rate-limited. Returns error message or None if OK."""
    now = time.time()
    entry = _login_attempts.get(ip)
    if not entry:
        return None
    if entry.get("locked_until", 0) > now:
        remaining = int(entry["locked_until"] - now)
        return f"Too many failed attempts. Try again in {remaining}s."
    if now - entry["first"] > _LOGIN_WINDOW:
        del _login_attempts[ip]
        return None
    return None


def _record_login_failure(ip: str):
    """Record a failed login attempt from an IP."""
    now = time.time()
    entry = _login_attempts.get(ip)
    if not entry or (now - entry["first"]) > _LOGIN_WINDOW:
        _login_attempts[ip] = {"count": 1, "first": now}
        return
    entry["count"] += 1
    if entry["count"] >= _LOGIN_MAX_ATTEMPTS:
        entry["locked_until"] = now + _LOGIN_LOCKOUT
        logger.warning(f"Login lockout for {ip} after {entry['count']} failed attempts")


def _clear_login_attempts(ip: str):
    """Clear rate limit tracking after successful login."""
    _login_attempts.pop(ip, None)


async def _cleanup_sessions_loop():
    """Periodically prune expired sessions and stale login attempt entries."""
    while True:
        await asyncio.sleep(3600)  # every hour
        now_ts = time.time()
        now_dt = datetime.now(timezone.utc)
        # Prune expired sessions
        expired = [
            tok for tok, sess in _sessions.items()
            if (now_dt - datetime.fromisoformat(sess["created"])).total_seconds() > SESSION_EXPIRY
        ]
        for tok in expired:
            _sessions.pop(tok, None)
        # Prune stale login attempt entries
        stale = [
            ip for ip, entry in _login_attempts.items()
            if now_ts - entry["first"] > _LOGIN_WINDOW and entry.get("locked_until", 0) < now_ts
        ]
        for ip in stale:
            _login_attempts.pop(ip, None)
        if expired or stale:
            logger.info(f"Session cleanup: {len(expired)} expired sessions, {len(stale)} stale login entries")


def _get_user_role(auth_id: str) -> str:
    """Get the role for an authenticated user. Returns 'admin', 'viewer', or 'key'."""
    if auth_id.startswith("session:"):
        username = auth_id.split(":", 1)[1]
        users = _load_users()
        return users.get(username, {}).get("role", "viewer")
    return "key"


def _mask_key(key: str) -> str:
    """Mask an API key, showing only last 8 characters."""
    if len(key) <= 8:
        return "****"
    return "****" + key[-8:]


# Create default users on import
_create_default_users()

# Load provider keys from env (preferred) or data/config.json (server-side only).
# These are upstream credentials — never exposed to clients.
_MINIMAX_KEY = os.environ.get("MINIMAX_API_KEY", "")
_ZAI_KEY = os.environ.get("ZAI_API_KEY", "")
_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

_local_cfg_path = os.path.join(_DATA_DIR, "config.json")
if os.path.isfile(_local_cfg_path):
    try:
        with open(_local_cfg_path) as _f:
            _oc = json.load(_f)
        _providers = _oc.get("models", {}).get("providers", {}) or _oc.get("providers", {})
        if not _MINIMAX_KEY:
            _MINIMAX_KEY = _providers.get("minimax", {}).get("apiKey", "")
        if not _ZAI_KEY:
            _ZAI_KEY = _providers.get("zai", {}).get("apiKey", "") or _providers.get("z.ai", {}).get("apiKey", "")
        if not _OPENAI_KEY:
            _OPENAI_KEY = _providers.get("openai", {}).get("apiKey", "") or _providers.get("codex", {}).get("apiKey", "")
        if not _GEMINI_KEY:
            _GEMINI_KEY = _providers.get("gemini", {}).get("apiKey", "")
    except Exception:
        pass

# ── Startup Validation ──
_startup_warnings = []
if not _MINIMAX_KEY:
    _startup_warnings.append("MiniMax API key not found — /api/minimax will return 503")
if _MINIMAX_KEY and _MINIMAX_KEY.startswith("pcx-"):
    _startup_warnings.append("MiniMax API key looks like a proxy key (pcx-*), not a real MiniMax key (sk-*)")
# Verify upstream is reachable (non-blocking warning)
try:
    import urllib.request
    urllib.request.urlopen(f"{PROXY_TARGET}/health", timeout=3)
    logger.info("[STARTUP] Claude API upstream is reachable")
except Exception:
    _startup_warnings.append(f"Claude API upstream ({PROXY_TARGET}) not reachable at startup")
for _w in _startup_warnings:
    logger.warning(f"[STARTUP] {_w}")
    print(f"[STARTUP WARNING] {_w}")

# ── Unified Error Response ──
def _error_response(status_code: int, error_code: str, message: str, request: Optional[Request] = None, details: Optional[dict] = None) -> JSONResponse:
    """Return a consistent error response across all proxy endpoints."""
    req_id = ""
    if request:
        req_id = getattr(request.state, "request_id", "") or request.headers.get("x-request-id", "")
    body = {
        "error": error_code,
        "message": message,
        "request_id": req_id,
    }
    if details:
        body["details"] = details
    headers = {}
    if req_id:
        headers["X-Request-ID"] = req_id
    return JSONResponse(content=body, status_code=status_code, headers=headers)

# Headers to strip for privacy (never forwarded to upstream)
STRIP_HEADERS = {
    "x-forwarded-for", "x-real-ip", "x-forwarded-proto", "x-forwarded-host",
    "forwarded", "via", "x-envoy-external-address",
}

# Headers allowed to pass through to upstream (localhost only — never external).
# Add custom headers here; everything else is dropped for privacy.
PASSTHROUGH_HEADERS = {
    "content-type", "anthropic-version", "accept",
    "x-agent-id", "x-source", "x-space-id", "x-user-id",
}

router = APIRouter(prefix="/api", tags=["api"])


# ═══ Tailscale Node Tracking (replaces CF-IPCountry geo tracking) ═══

_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Node identity cache: IP -> {name, os, online, last_checked}
_ts_node_cache = {}
_ts_cache_lock = threading.Lock()


_ts_status_cache = {"data": {}, "ts": 0}

def _get_tailscale_status() -> dict:
    """Get Tailscale status. Cached for 30s to avoid repeated subprocess calls."""
    now = time.time()
    if (now - _ts_status_cache["ts"]) < 30 and _ts_status_cache["data"]:
        return _ts_status_cache["data"]
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            _ts_status_cache["data"] = data
            _ts_status_cache["ts"] = now
            return data
    except Exception:
        pass
    return {}


def _get_tailscale_node(ip: str) -> dict:
    """Look up a Tailscale node by IP. Cached for 5 minutes."""
    with _ts_cache_lock:
        cached = _ts_node_cache.get(ip)
        if cached and (time.time() - cached.get("last_checked", 0)) < 300:
            return cached

    status = _get_tailscale_status()
    if not status:
        return {"name": "unknown", "os": "", "online": True, "last_checked": time.time()}

    # Check peers
    for _peer_id, peer in status.get("Peer", {}).items():
        for addr in peer.get("TailscaleIPs", []):
            if addr == ip:
                node_info = {
                    "name": peer.get("HostName", "unknown"),
                    "os": peer.get("OS", ""),
                    "online": peer.get("Online", False),
                    "last_checked": time.time(),
                }
                with _ts_cache_lock:
                    _ts_node_cache[ip] = node_info
                return node_info

    # Check self
    self_node = status.get("Self", {})
    for addr in self_node.get("TailscaleIPs", []):
        if addr == ip:
            node_info = {
                "name": self_node.get("HostName", "mac-mini"),
                "os": self_node.get("OS", ""),
                "online": True,
                "last_checked": time.time(),
            }
            with _ts_cache_lock:
                _ts_node_cache[ip] = node_info
            return node_info

    return {"name": "unknown", "os": "", "online": True, "last_checked": time.time()}


def _get_node_coords(node_name: str) -> dict:
    """Get map coordinates for a Tailscale node from config.
    Configure node lat/lng in data/tailscale.json under 'nodes'."""
    try:
        if os.path.isfile(TAILSCALE_CONFIG):
            with open(TAILSCALE_CONFIG) as f:
                config = json.load(f)
            nodes = config.get("nodes", {})
            if node_name in nodes:
                return nodes[node_name]
    except Exception:
        pass
    # Unknown node: return 0,0 placeholder so the dashboard map still renders.
    return {"lat": 0.0, "lng": 0.0, "name": node_name}


_geo_lock = threading.Lock()
_geo_data = {
    "by_country": {},   # node_name -> {count, last_seen}
    "pings": [],        # last 200 pings [{cc, endpoint, ts, lat, lng, name}]
}


def _load_geo():
    """Load persisted geo data on startup."""
    global _geo_data
    if os.path.isfile(GEO_LOG_FILE):
        try:
            with open(GEO_LOG_FILE) as f:
                _geo_data = json.load(f)
        except Exception:
            pass


def _save_geo():
    """Persist geo data to disk (called periodically, not every request)."""
    try:
        _atomic_write_json(GEO_LOG_FILE, _geo_data, indent=None)
    except Exception:
        pass


def _record_source(request: Request, endpoint: str):
    """Record request source. Uses Tailscale node identity or 'local' for localhost."""
    client = request.client
    source_id = "local"
    source_name = "Mac Mini"

    if client and not _is_local(request):
        ip = client.host
        if _is_tailscale(request):
            node = _get_tailscale_node(ip)
            source_id = node["name"]
            source_name = node["name"]
        else:
            source_id = "unknown"
            source_name = "Unknown"

    now = datetime.now(timezone.utc).isoformat()
    coords = _get_node_coords(source_id)

    with _geo_lock:
        if source_id not in _geo_data["by_country"]:
            _geo_data["by_country"][source_id] = {"count": 0, "last_seen": now}
        _geo_data["by_country"][source_id]["count"] += 1
        _geo_data["by_country"][source_id]["last_seen"] = now

        _geo_data["pings"].append({
            "cc": source_id,
            "endpoint": endpoint,
            "ts": now,
            "lat": coords.get("lat", -33.9),
            "lng": coords.get("lng", 151.2),
            "name": coords.get("name", source_id),
        })
        if len(_geo_data["pings"]) > 200:
            _geo_data["pings"] = _geo_data["pings"][-200:]

        total = sum(c["count"] for c in _geo_data["by_country"].values())
        if total % 10 == 0:
            _save_geo()


# Load on import
_load_geo()


_keys_cache = {"data": {}, "mtime": 0}

def _load_keys() -> dict:
    """Load API keys from disk. Cached with mtime check for hot-reload without repeated I/O."""
    if not os.path.isfile(KEYS_FILE):
        return {}
    try:
        mtime = os.path.getmtime(KEYS_FILE)
        if mtime == _keys_cache["mtime"] and _keys_cache["data"]:
            return _keys_cache["data"]
        with open(KEYS_FILE) as f:
            data = json.load(f)
        result = data.get("keys", {})
        _keys_cache["data"] = result
        _keys_cache["mtime"] = mtime
        return result
    except Exception:
        return {}


def _save_keys(keys: dict):
    """Save API keys to disk."""
    _atomic_write_json(KEYS_FILE, {"keys": keys}, mode=0o600)


def _authenticate(request: Request) -> str:
    """Validate API key, session cookie, or Bearer token. Returns key_id/username or raises 401."""
    # Check session cookie first (dashboard browser sessions)
    username = _validate_session(request)
    if username:
        return f"session:{username}"

    provided = request.headers.get("x-api-key", "")
    # Also accept Authorization: Bearer (session tokens or API keys)
    if not provided:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:]
    # Query param auth removed for security (keys leak to logs/history)
    if not provided:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Check if it's a session token
    if provided in _sessions:
        sess = _sessions[provided]
        created = datetime.fromisoformat(sess["created"])
        if (datetime.now(timezone.utc) - created).total_seconds() <= SESSION_EXPIRY:
            return f"session:{sess['username']}"
        del _sessions[provided]

    # Check API keys
    keys = _load_keys()
    for key_id, info in keys.items():
        if info.get("key") == provided:
            return key_id

    raise HTTPException(status_code=401, detail="Invalid credentials")


def _clean_headers(request: Request, key_id: str = "") -> dict:
    """Build clean headers for upstream — only allowed headers pass through.
    Injects x-source and x-source-key so upstream can attribute requests."""
    clean = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in PASSTHROUGH_HEADERS:
            clean[name] = value
    # Inject source attribution headers for upstream analytics
    if key_id:
        clean["x-source-key"] = key_id
        clean["x-source"] = f"proxy:{key_id}" if not key_id.startswith("session:") else "dashboard"
    elif _is_local(request):
        clean["x-source"] = "direct"
    elif _is_tailscale(request):
        clean["x-source"] = "proxy:tailscale"
    else:
        clean["x-source"] = "proxy:unknown"
    return clean


def _is_local(request: Request) -> bool:
    """Check if request originates from localhost."""
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")


def _is_tailscale(request: Request) -> bool:
    """Check if request comes from a Tailscale peer (100.64.0.0/10 CGNAT range)."""
    client = request.client
    if not client:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
        return ip in _TAILSCALE_CGNAT
    except ValueError:
        return False


def _is_trusted(request: Request) -> bool:
    """Check if request is from localhost or a Tailscale peer."""
    return _is_local(request) or _is_tailscale(request)


# ═══ Request Lifecycle Tracking ═══

def _start_request(endpoint: str, key_id: str = "", model: str = "", extra: dict = None) -> str:
    """Mark a request as started. Returns a request ID."""
    global _request_counter
    with _request_log_lock:
        _request_counter += 1
        req_id = f"r{_request_counter}"
    info = {
        "endpoint": endpoint,
        "started": time.time(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key_id": key_id,
        "model": model,
    }
    if extra:
        info.update(extra)
    with _request_log_lock:
        _active_requests[req_id] = info
    return req_id


def _end_request(req_id: str, status: str = "ok", output_tokens: int = 0):
    with _request_log_lock:
        info = _active_requests.pop(req_id, None)
    if info:
        elapsed = time.time() - info["started"]
        # NEW: record in the recent-requests buffer
        with _request_log_lock:
            _recent_requests.append({
                "ts": info.get("started_at", ""),
                "endpoint": info.get("endpoint", ""),
                "model": info.get("model", ""),
                "source": info.get("key_id", "local"),
                "input_tokens": info.get("input_tokens_est", 0),
                "output_tokens": output_tokens,
                "elapsed": round(elapsed, 2),
                "status": status,
            })
        if elapsed > 30:
            logger.warning(
                f"SLOW {info['endpoint']} model={info.get('model','')} "
                f"elapsed={elapsed:.0f}s key={info.get('key_id','')}"
            )

def _fail_request(req_id: str, error_type: str, detail: str, status_code: int = 0):
    """Record a failed request with full detail."""
    with _request_log_lock:
        info = _active_requests.pop(req_id, None)
    elapsed = (time.time() - info["started"]) if info else 0
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "endpoint": info.get("endpoint", "") if info else "",
        "model": info.get("model", "") if info else "",
        "key_id": info.get("key_id", "") if info else "",
        "elapsed": round(elapsed, 1),
        "error_type": error_type,
        "detail": detail[:500],
        "status_code": status_code,
    }
    with _request_log_lock:
        _error_log.append(entry)
        _recent_requests.append({
            "ts": entry["ts"],
            "endpoint": entry["endpoint"],
            "model": entry["model"],
            "source": entry["key_id"] or "local",
            "input_tokens": 0,
            "output_tokens": 0,
            "elapsed": entry["elapsed"],
            "status": "error",
        })
    logger.error(
        f"FAIL {entry['endpoint']} type={error_type} status={status_code} "
        f"elapsed={elapsed:.0f}s detail={detail[:200]}"
    )  
# ═══ API Help / Docs ═══

API_ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/messages", "auth": True,
     "description": "Anthropic-compatible messages endpoint. Supports SSE streaming. Use this for Claude Code / Anthropic SDK.",
     "body": '{"model": "claude-sonnet-4-5-20250929", "max_tokens": 1024, "messages": [{"role": "user", "content": "Hello"}]}'},
    {"method": "POST", "path": "/api/opus", "auth": True,
     "description": "Send a prompt to Claude Opus. Simple text in, text out.",
     "body": '{"prompt": "Your prompt here"}'},
    {"method": "POST", "path": "/api/sonnet", "auth": True,
     "description": "Send a prompt to Claude Sonnet. Simple text in, text out.",
     "body": '{"prompt": "Your prompt here"}'},
    {"method": "POST", "path": "/api/prompt", "auth": True,
     "description": "Send a prompt with auto-selected model. Defaults to Opus.",
     "body": '{"prompt": "Your prompt here", "model": "opus"}'},
    {"method": "POST", "path": "/api/minimax", "auth": True,
     "description": "MiniMax M2.5 chat completions (OpenAI-compatible format).",
     "body": '{"model": "MiniMax-M2.5", "messages": [{"role": "user", "content": "Hello"}]}'},
    {"method": "POST", "path": "/api/zai", "auth": True,
     "description": "Z.AI GLM-4.6 chat (Anthropic /v1/messages compatible). Drop-in cascade target for Claude.",
     "body": '{"model": "glm-4.6", "max_tokens": 1024, "messages": [{"role": "user", "content": "Hello"}]}'},
    {"method": "POST", "path": "/api/codex", "auth": True,
     "description": "Codex/GPT chat. Routes to local codex CLI by default (uses ChatGPT subscription); set CODEX_MODE=api with OPENAI_API_KEY for direct API mode.",
     "body": '{"model": "gpt-5", "max_tokens": 1024, "messages": [{"role": "user", "content": "Hello"}]}'},
    {"method": "POST", "path": "/api/suggest", "auth": True,
     "description": "Generate suggestions via MiniMax M2.5.",
     "body": '{"prompt": "Your prompt here"}'},
    {"method": "POST", "path": "/api/generate", "auth": True,
     "description": "Generate long-form content via MiniMax M2.5.",
     "body": '{"prompt": "Your prompt here"}'},
    {"method": "POST", "path": "/api/image", "auth": True,
     "description": "Generate images via Gemini Flash (set GEMINI_API_KEY).",
     "body": '{"prompt": "Your prompt here"}'},
    {"method": "GET", "path": "/api/health", "auth": False,
     "description": "Health check. Returns server status and uptime."},
    {"method": "GET", "path": "/api/stats", "auth": True,
     "description": "Usage statistics: request counts, tokens, hourly activity."},
    {"method": "GET", "path": "/api/limits", "auth": True,
     "description": "Current rate limit usage vs configured limits."},
    {"method": "GET", "path": "/api/geo", "auth": True,
     "description": "Source tracking data for the dashboard map."},
    {"method": "GET", "path": "/api/network-status", "auth": True,
     "description": "Tailscale network status. Returns hostname, IP, and peer count."},
    {"method": "GET", "path": "/api/setup", "auth": True,
     "description": "Connection info for setting up Claude Code on a remote server. Admin only."},
    {"method": "GET", "path": "/api/errors", "auth": "trusted",
     "description": "Recent errors with full detail. Shows active in-flight requests."},
    {"method": "GET", "path": "/api/active", "auth": "trusted",
     "description": "Currently in-flight requests with elapsed time."},
    {"method": "GET", "path": "/api/monitor", "auth": "trusted",
     "description": "Full service health diagnostic report."},
    {"method": "GET", "path": "/api/help", "auth": False,
     "description": "This endpoint. Lists all available API endpoints."},
    {"method": "POST", "path": "/api/auth", "auth": False,
     "description": "Login with username/password. Returns a session token.",
     "body": '{"username": "...", "password": "..."}'},
]


@router.get("/help")
async def api_help():
    """Return a machine-readable list of all API endpoints."""
    return JSONResponse(content={
        "name": "Agent Router API",
        "auth": "Send API key via x-api-key header or Authorization: Bearer header.",
        "transport": "Tailscale (WireGuard mesh) recommended; LAN/loopback also supported",
        "endpoints": API_ENDPOINTS,
    })


# ═══ Model Endpoints ═══


@router.post("/v1/messages")
async def handle_messages(request: Request):
    """Anthropic-compatible /v1/messages.

    Routing (free-first): Ollama → Hugging Face → OpenRouter.
    Claude remains reachable via /opus, /sonnet, /prompt.
    """
    key_id = _authenticate(request)
    _record_source(request, "/v1/messages")
    body = await request.body()

    try:
        payload = json.loads(body)
    except Exception:
        return _error_response(400, "invalid_json",
                               "Request body is not valid JSON", request)

    model = payload.get("model", "")
    is_stream = bool(payload.get("stream", False))

    limit_err = _check_rate_limit("/v1/messages", model)
    if limit_err:
        return _error_response(429, "rate_limited", limit_err, request)

    space_id = request.headers.get("x-space-id", "")
    user_id = request.headers.get("x-user-id", "")
    req_id = _start_request("/v1/messages", key_id=key_id, model=model, extra={
        "stream": is_stream,
        "input_tokens_est": len(body) // 4,
        "space_id": space_id,
        "user_id": user_id,
        "routing": "free_cascade",
    })

    _track_usage("/v1/messages", model)

    try:
        if is_stream:
            resp = await cascade_stream(payload, req_id=req_id)
            if resp is None:
                _fail_request(req_id, "cascade_exhausted",
                              "All free providers unavailable (streaming)", 503)
                return _error_response(
                    503, "cascade_exhausted",
                    "All configured free providers are unavailable.", request,
                )
            _end_request(req_id, status="ok_stream")
            return resp

        result = await cascade_request(payload, req_id=req_id)
        if result is None:
            _fail_request(req_id, "cascade_exhausted",
                          "All free providers unavailable", 503)
            return _error_response(
                503, "cascade_exhausted",
                "All configured free providers are unavailable.", request,
            )
        _end_request(req_id, status="ok")
        return result

    except HTTPException as e:
        _fail_request(req_id, "http_exception", str(e.detail), e.status_code)
        raise
    except Exception as e:
        _fail_request(req_id, type(e).__name__, str(e), 502)
        return _error_response(502, "provider_cascade_failed",
                               "Provider cascade failed", request)

@router.post("/opus")
async def handle_opus(request: Request):
    """Send prompt to Claude Opus."""
    key_id = _authenticate(request)
    _record_source(request, "/opus")
    _track_usage("/opus")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    req_id = _start_request("/opus", key_id=key_id, model="opus")
    try:
        await _acquire_queue_slot(request, req_id)
    except HTTPException:
        _fail_request(req_id, "queue_timeout", "Timed out waiting in queue", 503)
        raise
    try:
        return await _forward_with_retry("/opus", body, headers, req_id=req_id)
    finally:
        _release_queue_slot()


@router.post("/sonnet")
async def handle_sonnet(request: Request):
    """Send prompt to Claude Sonnet."""
    key_id = _authenticate(request)
    _record_source(request, "/sonnet")
    _track_usage("/sonnet")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    req_id = _start_request("/sonnet", key_id=key_id, model="sonnet")
    try:
        await _acquire_queue_slot(request, req_id)
    except HTTPException:
        _fail_request(req_id, "queue_timeout", "Timed out waiting in queue", 503)
        raise
    try:
        return await _forward_with_retry("/sonnet", body, headers, req_id=req_id)
    finally:
        _release_queue_slot()


@router.post("/prompt")
async def handle_prompt(request: Request):
    """Send prompt with auto-selected model."""
    key_id = _authenticate(request)
    _record_source(request, "/prompt")
    _track_usage("/prompt")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    req_id = _start_request("/prompt", key_id=key_id, model="prompt")
    try:
        await _acquire_queue_slot(request, req_id)
    except HTTPException:
        _fail_request(req_id, "queue_timeout", "Timed out waiting in queue", 503)
        raise
    try:
        return await _forward_with_retry("/prompt", body, headers, req_id=req_id)
    finally:
        _release_queue_slot()


@router.post("/suggest")
async def handle_suggest(request: Request):
    """Generate suggestions via MiniMax."""
    key_id = _authenticate(request)
    _record_source(request, "/suggest")
    _track_usage("/suggest")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    return await _forward("/suggest", body, headers)


@router.post("/generate")
async def handle_generate(request: Request):
    """Generate long-form content via MiniMax."""
    key_id = _authenticate(request)
    _record_source(request, "/generate")
    _track_usage("/generate")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    return await _forward("/writer-generate", body, headers)


@router.post("/image")
async def handle_image(request: Request):
    """Generate images via Gemini Flash."""
    key_id = _authenticate(request)
    _record_source(request, "/image")
    _track_usage("/image")
    body = await request.body()
    headers = _clean_headers(request, key_id=key_id)
    return await _forward("/generate-cover", body, headers)


# ═══ MiniMax (OpenAI-compatible) ═══

# Valid MiniMax roles and top-level keys
_MINIMAX_VALID_ROLES = {"system", "user", "assistant"}
_MINIMAX_VALID_KEYS = {
    "model", "messages", "max_tokens", "temperature", "top_p", "stream",
    "stop", "n", "presence_penalty", "frequency_penalty", "tools",
    "tool_choice", "stream_options", "reasoning_split",
}


def _sanitize_minimax_payload(payload: dict) -> dict:
    """Translate incoming app request into a clean MiniMax-compatible payload.

    - Rewrites unsupported roles (developer → system)
    - Flattens structured content blocks to plain text
    - Strips Anthropic-specific fields (cache_control, citations, etc.)
    - Removes unknown top-level parameters
    - Merges consecutive same-role messages
    """
    out = {k: v for k, v in payload.items() if k in _MINIMAX_VALID_KEYS}
    out["model"] = payload.get("model", "MiniMax-M2.5")

    clean_msgs = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        # Map unsupported roles
        if role not in _MINIMAX_VALID_ROLES:
            role = "system" if role == "developer" else "user"

        # Flatten content: structured blocks → plain text
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content", block.get("output", ""))))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)

        if not content:
            continue

        # Merge consecutive same-role messages
        if clean_msgs and clean_msgs[-1]["role"] == role:
            clean_msgs[-1]["content"] += "\n" + content
        else:
            clean_msgs.append({"role": role, "content": content})

    out["messages"] = clean_msgs
    return out


def _anthropic_to_minimax(payload: dict) -> dict:
    """Convert Anthropic /v1/messages payload to MiniMax-compatible format."""
    messages = []
    # Convert system prompt to system message
    system = payload.get("system", "")
    if system:
        if isinstance(system, list):
            system = "\n".join(b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in system)
        messages.append({"role": "system", "content": system})
    # Convert messages
    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        if role not in _MINIMAX_VALID_ROLES:
            role = "user"
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content", block.get("output", ""))))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
        if not content:
            continue
        # Merge consecutive same-role
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + content
        else:
            messages.append({"role": role, "content": content})
    out = {
        "model": "MiniMax-M2.5",
        "messages": messages,
        "stream": payload.get("stream", False),
    }
    if payload.get("max_tokens"):
        out["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    return out


async def _cascade_to_minimax(payload: dict, req_id: str = "",
                               is_stream: bool = False,
                               request: Request = None,
                               on_complete=None) -> Optional[Response]:
    """Cascade a Claude request to MiniMax when rate limited.
    Returns a Response on success, None if MiniMax is also unavailable."""
    if not _MINIMAX_KEY:
        return None
    # Check MiniMax rate limit too — don't cascade if also exhausted
    mm_limit_err = _check_rate_limit("/minimax")
    if mm_limit_err:
        logger.info(f"CASCADE-SKIP {req_id} MiniMax also rate limited")
        return None
    mm_payload = _anthropic_to_minimax(payload)
    mm_body = json.dumps(mm_payload).encode()
    mm_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_MINIMAX_KEY}",
    }
    _track_usage("/minimax")
    logger.info(f"CASCADE {req_id} Claude rate limited → MiniMax fallback"
                f" stream={is_stream}")
    try:
        if is_stream:
            mm_payload["stream"] = True
            mm_body = json.dumps(mm_payload).encode()
            stream_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout=cfg_timeout("minimax"), connect=_connect_timeout)
            )

            async def cascade_stream():
                try:
                    async with stream_client.stream(
                        "POST",
                        f"{MINIMAX_TARGET}/chat/completions",
                        content=mm_body,
                        headers=mm_headers,
                    ) as resp:
                        if resp.status_code >= 400:
                            error_body = b""
                            async for chunk in resp.aiter_bytes():
                                error_body += chunk
                            logger.warning(f"CASCADE-ERR {req_id} MiniMax returned {resp.status_code}")
                            yield error_body
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                finally:
                    await stream_client.aclose()
                    if on_complete:
                        on_complete()

            return StreamingResponse(
                cascade_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Cascade": "minimax",
                },
            )
        else:
            resp = await _minimax_client.post(
                f"{MINIMAX_TARGET}/chat/completions",
                content=mm_body,
                headers=mm_headers,
            )
            if resp.status_code >= 400:
                logger.warning(f"CASCADE-ERR {req_id} MiniMax returned {resp.status_code}")
                return None
            data = resp.json()
            # Convert MiniMax OpenAI response to Anthropic format
            choices = data.get("choices", [])
            content_text = choices[0]["message"]["content"] if choices else ""
            anthropic_resp = {
                "id": data.get("id", req_id),
                "type": "message",
                "role": "assistant",
                "model": "minimax-m2-5-cascade",
                "content": [{"type": "text", "text": f"(cascaded to MiniMax — Claude rate limited)\n\n{content_text}"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                    "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
                },
            }
            if req_id:
                _end_request(req_id, status="cascaded_minimax")
            return JSONResponse(content=anthropic_resp, status_code=200,
                                headers={"X-Cascade": "minimax"})
    except Exception as e:
        logger.warning(f"CASCADE-ERR {req_id} MiniMax error: {type(e).__name__}: {e}")
        return None


def _build_deliberation_payload(original_payload: dict, previous_response: str,
                                 round_num: int, total_rounds: int) -> dict:
    """Build payload for deliberation round with context from previous round."""
    payload = original_payload.copy()

    # Get existing messages
    messages = payload.get("messages", [])

    # Add previous response as context if not first round
    if previous_response and round_num > 1:
        context_message = {
            "role": "assistant",
            "content": f"[Previous deliberation round {round_num-1}]\n\n{previous_response}"
        }
        messages.append(context_message)

    # Modify system prompt for deliberation
    system = payload.get("system", "")
    deliberation_prompt = f"[Deliberation Round {round_num}/{total_rounds}. Review and improve the previous response. Focus on accuracy, clarity, and completeness.]"

    if system:
        payload["system"] = f"{system}\n\n{deliberation_prompt}"
    else:
        payload["system"] = deliberation_prompt

    payload["messages"] = messages

    return payload


async def _deliberation_execute(req_config: dict, original_payload: dict,
                                  body: bytes, headers: dict):
    """Execute deliberation mode: sequential workers with context passing."""
    rounds = req_config.get("deliberation_rounds", 3)
    target = MINIMAX_TARGET

    req_id = generate_request_id()
    deliberation_id = f"deliberation-{int(time.time()*1000)}-{_request_counter}"
    deliberation_start = time.time()

    with _swarm_lock:
        _swarm_stats["total_swarms"] += 1
        _swarm_stats["active_swarms"] += 1

    last_response = None
    final_result = None

    try:
        for round_num in range(1, rounds + 1):
            r_start = time.time()

            # Build deliberation payload with context
            payload = _build_deliberation_payload(
                original_payload,
                last_response,
                round_num,
                rounds
            )

            # Execute single worker
            resp = await _minimax_client.post(
                f"{target}/chat/completions",
                content=json.dumps(payload).encode(),
                headers=headers,
            )

            elapsed = (time.time() - r_start) * 1000

            if resp.status_code >= 400:
                # Return last successful response if this round failed
                error_body = resp.text[:500]
                if final_result:
                    final_result["deliberation_error"] = f"Round {round_num} failed: {error_body}"
                    logger.warning(f"DELIBERATION-ROUND-FAIL {deliberation_id} round={round_num} error={error_body}")
                    break
                else:
                    logger.error(f"DELIBERATION-FAIL {deliberation_id} round={round_num} error={error_body}")
                    raise HTTPException(status_code=502, detail=f"Deliberation round {round_num} failed: {error_body}")

            result = resp.json()
            final_result = result
            _update_provider_limits("minimax", dict(resp.headers))

            # Extract response content for next round
            choices = result.get("choices", [])
            if choices:
                last_response = choices[0].get("message", {}).get("content", "")

            # Track usage for this round
            _track_usage(target, worker_count=1)

            # Track tokens if available
            usage = result.get("usage", {})
            if usage:
                _track_tokens(target, original_payload.get("model", "MiniMax-M2.5"),
                            usage.get("input_tokens", 0), usage.get("output_tokens", 0))

            logger.info(f"DELIBERATION-ROUND {deliberation_id} round={round_num}/{rounds} ms={int(elapsed)}")

        # Add deliberation metadata to final response
        if final_result:
            final_result["deliberation"] = {
                "id": deliberation_id,
                "rounds": rounds,
                "completed": True,
                "total_ms": int((time.time() - deliberation_start) * 1000)
            }

        return JSONResponse(content=final_result, status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DELIBERATION-ERR {deliberation_id} {type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Deliberation error: {type(e).__name__}")
    finally:
        with _swarm_lock:
            _swarm_stats["active_swarms"] -= 1


async def _swarm_stream(request: Request, req_config: dict, original_payload: dict,
                        body: bytes, headers: dict) -> StreamingResponse:
    """Execute swarm with streaming responses from all workers."""
    swarm_mode = req_config["swarm_mode"]
    swarm_workers = req_config["swarm_workers"]

    # For deliberation mode, streaming not supported yet - fall back to non-streaming
    if swarm_mode == "deliberation":
        # Return error - deliberation with streaming not supported
        raise HTTPException(
            status_code=400,
            detail="Deliberation mode with streaming not yet supported. Use mode=parallel or mode=none."
        )

    req_id = generate_request_id()
    swarm_id = f"swarm-stream-{int(time.time()*1000)}-{_request_counter}"
    swarm_start = time.time()

    with _swarm_lock:
        _swarm_stats["total_swarms"] += 1
        _swarm_stats["active_swarms"] += 1

    total_input_tokens = 0
    total_output_tokens = 0

    async def stream_generator():
        nonlocal total_input_tokens, total_output_tokens
        try:
            # Create streaming requests for each worker
            async def run_stream_worker(worker_id: int):
                """Run a single streaming worker."""
                w_start = time.time()
                try:
                    req = _minimax_client.build_request(
                        "POST",
                        f"{MINIMAX_TARGET}/chat/completions",
                        content=body,
                        headers=headers,
                    )
                    resp = await _minimax_client.send(req, stream=True)
                    elapsed = (time.time() - w_start) * 1000

                    with _swarm_lock:
                        _swarm_stats["total_workers_run"] += 1

                    if resp.status_code >= 400:
                        error_body = await resp.aread()
                        error_text = error_body.decode("utf-8", errors="replace")[:300]
                        with _swarm_lock:
                            _swarm_stats["worker_failures"] += 1
                        return {
                            "status": "error",
                            "worker": worker_id,
                            "error": error_text,
                            "ms": elapsed,
                            "chunks": [],
                        }

                    # Collect chunks from this worker
                    chunks = []
                    worker_input_tokens = 0
                    worker_output_tokens = 0

                    async for chunk in resp.aiter_bytes():
                        chunks.append(chunk)
                        # Try to extract token usage from SSE
                        try:
                            text = chunk.decode("utf-8", errors="replace")
                            for line in text.split("\n"):
                                if line.startswith("data: "):
                                    try:
                                        d = json.loads(line[6:])
                                        if d.get("type") == "message_start":
                                            u = d.get("message", {}).get("usage", {})
                                            worker_input_tokens += u.get("input_tokens", 0)
                                        elif d.get("type") == "message_delta":
                                            u = d.get("usage", {})
                                            worker_output_tokens += u.get("output_tokens", 0)
                                    except (json.JSONDecodeError, KeyError):
                                        pass
                        except Exception:
                            pass

                    await resp.aclose()

                    with _swarm_lock:
                        _swarm_stats["workers_succeeded"] += 1

                    return {
                        "status": "ok",
                        "worker": worker_id,
                        "ms": elapsed,
                        "chunks": chunks,
                        "input_tokens": worker_input_tokens,
                        "output_tokens": worker_output_tokens,
                    }
                except Exception as e:
                    elapsed = (time.time() - w_start) * 1000
                    with _swarm_lock:
                        _swarm_stats["total_workers_run"] += 1
                        _swarm_stats["worker_failures"] += 1
                    return {
                        "status": "error",
                        "worker": worker_id,
                        "error": str(e),
                        "ms": elapsed,
                        "chunks": [],
                    }

            # Run all workers in parallel
            workers = [run_stream_worker(i) for i in range(swarm_workers)]
            results = await asyncio.gather(*workers)

            # Aggregate chunks from all workers
            successful = [r for r in results if r["status"] == "ok"]
            worker_ms = [r["ms"] for r in results]
            avg_w = sum(worker_ms) / len(worker_ms) if worker_ms else 0

            # Collect tokens from successful workers
            for r in successful:
                total_input_tokens += r.get("input_tokens", 0)
                total_output_tokens += r.get("output_tokens", 0)

            # Interleave chunks from all workers
            # Simple approach: yield chunks from first worker, then second, etc.
            for r in results:
                for chunk in r.get("chunks", []):
                    yield chunk

            # Log swarm completion (decrement in finally block to avoid race)
            total_ms = (time.time() - swarm_start) * 1000
            with _swarm_lock:
                _swarm_stats["avg_worker_ms"] = (
                    _swarm_stats["avg_worker_ms"] * 0.8 + avg_w * 0.2
                )
                _swarm_stats["recent"].append({
                    "id": swarm_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "workers": len(results),
                    "successful": len(successful),
                    "worker_ms": [round(m) for m in worker_ms],
                    "total_ms": round(total_ms),
                    "model": original_payload.get("model", "MiniMax-M2.5"),
                    "streaming": True,
                })

            # Track usage for each successful worker (multiply by worker count)
            _track_usage("/minimax/swarm-worker", worker_count=len(successful))

            # Track total tokens
            if total_input_tokens > 0 or total_output_tokens > 0:
                _track_tokens("/minimax", original_payload.get("model", "MiniMax-M2.5"),
                            total_input_tokens, total_output_tokens)

            # Handle partial failures
            if not successful:
                worker_errors = [f"w{r['worker']}:{r.get('error', '?')}" for r in results]
                logger.error(f"SWARM-STREAM-FAIL {swarm_id} all workers failed: {worker_errors}")
                yield b'data: {"type": "error", "error": {"type": "upstream_error", "message": "All swarm workers failed"}}\n\n'

        except Exception as e:
            logger.error(f"SWARM-STREAM-ERR {swarm_id} {type(e).__name__}: {e}")
            yield f'data: {{"type": "error", "error": {{"type": "proxy_error", "message": "Swarm stream error: {type(e).__name__}"}}}}\n\n'.encode()
        finally:
            with _swarm_lock:
                _swarm_stats["active_swarms"] -= 1

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"x-request-id": req_id, "x-swarm-id": swarm_id},
    )


async def _minimax_chat(request: Request):
    """MiniMax chat via swarm or single upstream based on headers."""
    if not _is_trusted(request):
        _authenticate(request)
    _record_source(request, "/minimax")

    # Parse per-request configuration headers
    req_config = _parse_request_headers(request)
    swarm_mode = req_config["swarm_mode"]
    swarm_workers = req_config["swarm_workers"]
    temperature = req_config["temperature"]
    max_tokens = req_config["max_tokens"]

    # Check rate limit with worker count for swarm
    # For mode "none", worker count is effectively 1
    effective_workers = swarm_workers if (swarm_mode != "none" and _is_swarm_enabled()) else 1
    limit_err = _check_rate_limit("/minimax", worker_count=effective_workers)
    if limit_err:
        raise HTTPException(status_code=429, detail=limit_err)

    # Track usage with worker count for swarm
    _track_usage("/minimax", worker_count=effective_workers)
    if not _MINIMAX_KEY:
        raise HTTPException(status_code=503, detail="Service unavailable")

    body = await request.body()
    original_payload = json.loads(body)

    # Validate messages - reject empty or invalid
    messages = original_payload.get("messages")
    if not messages or not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages is required and must be a non-empty array")
    # Check for empty content in last message (user message)
    if messages:
        last_msg = messages[-1]
        if isinstance(last_msg.get("content"), str) and not last_msg["content"].strip():
            raise HTTPException(status_code=400, detail="Message content cannot be empty")

    # Apply per-request overrides to payload
    if temperature is not None:
        original_payload["temperature"] = temperature
    if max_tokens is not None:
        original_payload["max_tokens"] = max_tokens

    # ── Translate app payload → MiniMax-compatible payload ──
    original_payload = _sanitize_minimax_payload(original_payload)

    body = json.dumps(original_payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_MINIMAX_KEY}",
    }

    # Mode "none" bypasses swarm - route to single upstream
    if swarm_mode == "none" or not _is_swarm_enabled():
        # Single upstream request (no swarm)
        req_id = generate_request_id()
        try:
            if original_payload.get("stream"):
                req = _minimax_client.build_request(
                    "POST",
                    f"{MINIMAX_TARGET}/chat/completions",
                    content=body,
                    headers=headers,
                )
                resp = await _minimax_client.send(req, stream=True)
                if resp.status_code >= 400:
                    error_body = await resp.aread()
                    detail = error_body.decode("utf-8", errors="replace")[:500]
                    await resp.aclose()
                    logger.warning(f"MINIMAX-STREAM-ERR {req_id} status={resp.status_code} body={detail}")
                    raise HTTPException(status_code=resp.status_code, detail=detail)

                async def stream_minimax():
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    finally:
                        await resp.aclose()

                return StreamingResponse(
                    stream_minimax(),
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "text/event-stream"),
                    headers={"x-request-id": req_id},
                )
            else:
                # Non-streaming single request
                resp = await _minimax_client.post(
                    f"{MINIMAX_TARGET}/chat/completions",
                    content=body,
                    headers=headers,
                )
                if resp.status_code >= 400:
                    raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
                return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.HTTPError as e:
            logger.error(f"MINIMAX-ERR {req_id} {type(e).__name__}: {e}")
            raise HTTPException(status_code=502, detail=f"MiniMax upstream error: {type(e).__name__}")

    # Swarm mode: deliberation vs parallel
    is_streaming = original_payload.get("stream", False)

    # Handle deliberation mode (non-streaming only)
    if swarm_mode == "deliberation":
        if is_streaming:
            # Deliberation with streaming not yet supported
            raise HTTPException(
                status_code=400,
                detail="Deliberation mode with streaming not yet supported. Use mode=parallel or mode=none."
            )
        return await _deliberation_execute(req_config, original_payload, body, headers)

    # Handle parallel mode
    if is_streaming:
        return await _swarm_stream(request, req_config, original_payload, body, headers)

    swarm_id = f"swarm-{int(time.time()*1000)}-{_request_counter}"
    swarm_start = time.time()

    with _swarm_lock:
        _swarm_stats["total_swarms"] += 1
        _swarm_stats["active_swarms"] += 1

    try:
        # Phase 1: Run 3 workers in parallel
        async def run_worker(worker_id: int):
            w_start = time.time()
            try:
                resp = await _minimax_client.post(
                    f"{MINIMAX_TARGET}/chat/completions",
                    content=body,
                    headers=headers,
                )
                result = resp.json()
                elapsed = (time.time() - w_start) * 1000
                _update_provider_limits("minimax", dict(resp.headers))
                # Check for MiniMax error responses (e.g. 401 auth errors returned as JSON)
                with _swarm_lock:
                    _swarm_stats["total_workers_run"] += 1
                if resp.status_code >= 400 or result.get("type") == "error" or result.get("error"):
                    err_msg = result.get("error", {})
                    if isinstance(err_msg, dict):
                        err_msg = err_msg.get("message", str(err_msg))
                    with _swarm_lock:
                        _swarm_stats["worker_failures"] += 1
                    return {"status": "error", "error": f"MiniMax {resp.status_code}: {err_msg}", "worker": worker_id, "ms": elapsed}
                with _swarm_lock:
                    _swarm_stats["workers_succeeded"] += 1
                return {"status": "ok", "response": result, "worker": worker_id, "ms": elapsed}
            except Exception as e:
                elapsed = (time.time() - w_start) * 1000
                with _swarm_lock:
                    _swarm_stats["total_workers_run"] += 1
                    _swarm_stats["worker_failures"] += 1
                return {"status": "error", "error": str(e), "worker": worker_id, "ms": elapsed}

        workers = [run_worker(i) for i in range(swarm_workers)]
        results = await asyncio.gather(*workers)

        worker_ms = [r["ms"] for r in results]
        successful = [r for r in results if r["status"] == "ok"]

        if not successful:
            worker_errors = [f"w{r['worker']}:{r.get('error','?')}" for r in results]
            logger.error(f"SWARM-FAIL {swarm_id} all workers failed: {worker_errors}")
            with _swarm_lock:
                _swarm_stats["active_swarms"] -= 1
            raise HTTPException(status_code=502, detail=f"All swarm workers failed: {worker_errors}")

        # If only 1 succeeded, return it directly (skip consolidation)
        if len(successful) == 1:
            final = successful[0]["response"]
            cons_ms = 0
        else:
            # Phase 2: Consolidation meeting
            worker_texts = []
            for i, r in enumerate(successful):
                content = r["response"].get("choices", [{}])[0].get("message", {}).get("content", "")
                worker_texts.append(f"=== Response {i+1} ===\n{content}")

            original_content = ""
            msgs = original_payload.get("messages", [])
            if msgs:
                original_content = msgs[-1].get("content", "")

            consolidation_prompt = (
                "You received a question and 3 independent AI agents each answered it separately. "
                "Review all responses below and synthesize the single best, most accurate, and complete answer. "
                "Take the strongest points from each, resolve any contradictions, and produce a clear final response. "
                "Do NOT mention that multiple agents were consulted — just give the final answer directly.\n\n"
                f"Original question/prompt:\n{original_content}\n\n"
                + "\n\n".join(worker_texts)
            )

            consolidation_payload = {
                **original_payload,
                "messages": [{"role": "user", "content": consolidation_prompt}],
            }

            cons_start = time.time()
            try:
                cons_resp = await _minimax_client.post(
                    f"{MINIMAX_TARGET}/chat/completions",
                    content=json.dumps(consolidation_payload).encode(),
                    headers=headers,
                )
                final = cons_resp.json()
                cons_ms = (time.time() - cons_start) * 1000
                _update_provider_limits("minimax", dict(cons_resp.headers))
            except Exception:
                final = successful[0]["response"]
                cons_ms = 0

            with _swarm_lock:
                _swarm_stats["consolidations"] += 1
                _swarm_stats["avg_consolidation_ms"] = (
                    _swarm_stats["avg_consolidation_ms"] * 0.8 + cons_ms * 0.2
                )

        total_ms = (time.time() - swarm_start) * 1000
        avg_w = sum(worker_ms) / len(worker_ms)

        with _swarm_lock:
            _swarm_stats["active_swarms"] -= 1
            _swarm_stats["avg_worker_ms"] = (
                _swarm_stats["avg_worker_ms"] * 0.8 + avg_w * 0.2
            )
            _swarm_stats["recent"].append({
                "id": swarm_id,
                "ts": datetime.now(timezone.utc).isoformat(),
                "workers": len(results),
                "successful": len(successful),
                "worker_ms": [round(m) for m in worker_ms],
                "consolidation_ms": round(cons_ms),
                "total_ms": round(total_ms),
                "model": original_payload.get("model", "MiniMax-M2.5"),
            })

        # Track usage for swarm workers (multiply by worker count)
        _track_usage("/minimax/swarm-worker", worker_count=len(successful))

        # Track tokens from all swarm responses (OpenAI format)
        for r in successful:
            u = r["response"].get("usage", {})
            if u:
                _track_tokens("/minimax", r["response"].get("model", ""),
                              u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        # Consolidation response tokens
        final_u = final.get("usage", {})
        if final_u:
            _track_tokens("/minimax", final.get("model", ""),
                          final_u.get("prompt_tokens", 0), final_u.get("completion_tokens", 0))

        return JSONResponse(content=final, status_code=200)

    except HTTPException:
        raise
    except Exception as e:
        with _swarm_lock:
            _swarm_stats["active_swarms"] -= 1
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/minimax")
async def handle_minimax(request: Request):
    """MiniMax chat completions (clean path)."""
    return await _minimax_chat(request)


@router.post("/minimax/chat/completions")
async def handle_minimax_compat(request: Request):
    """MiniMax chat completions (OpenAI-compatible)."""
    return await _minimax_chat(request)


@router.post("/minimax/models")
async def handle_minimax_models(request: Request):
    """List available MiniMax models."""
    _authenticate(request)
    headers = {"Authorization": f"Bearer {_MINIMAX_KEY}"}
    try:
        resp = await _minimax_client.get(f"{MINIMAX_TARGET}/models", headers=headers)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception:
        raise HTTPException(status_code=503, detail="Service unavailable")


# ═══ Z.AI (GLM) ═══
# Z.AI exposes an Anthropic-compatible /v1/messages endpoint, so this is a
# near-zero-overhead passthrough. The upstream URL is configured in platform.json.

@router.post("/zai")
async def handle_zai(request: Request):
    """Z.AI (GLM-4.6) chat — Anthropic /v1/messages compatible."""
    return await _zai_chat(request)


@router.post("/zai/v1/messages")
async def handle_zai_compat(request: Request):
    """Z.AI Anthropic-compatible alias."""
    return await _zai_chat(request)


async def _zai_chat(request: Request) -> Response:
    """Forward an Anthropic /v1/messages request to Z.AI's compatible endpoint."""
    key_id = _authenticate(request)
    if not _ZAI_KEY:
        return _error_response(503, "zai_unavailable",
                               "Z.AI key not configured (set ZAI_API_KEY)", request)

    body = await request.body()
    # Apply default model when caller hasn't specified one.
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return _error_response(400, "invalid_json", "Request body is not valid JSON", request)
    if not payload.get("model"):
        payload["model"] = cfg.get("providers", {}).get("zai", {}).get("default_model", "glm-4.6")
        body = json.dumps(payload).encode()

    is_stream = bool(payload.get("stream"))
    rate_err = _check_rate_limit("/zai", payload.get("model", ""))
    if rate_err:
        return _error_response(429, "rate_limited", rate_err, request)

    req_id = _start_request("/zai", key_id, payload.get("model", ""))
    _track_usage("/zai", payload.get("model", ""))

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_ZAI_KEY}",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }

    try:
        if is_stream:
            async def zai_stream():
                try:
                    async with _zai_client.stream(
                        "POST", f"{ZAI_TARGET}/v1/messages",
                        content=body, headers=headers,
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    _end_request(req_id, "ok")
                except Exception as e:
                    _fail_request(req_id, "stream_error", str(e))
                    raise
            return StreamingResponse(zai_stream(), media_type="text/event-stream")

        resp = await _zai_client.post(
            f"{ZAI_TARGET}/v1/messages", content=body, headers=headers,
        )
        _end_request(req_id, "ok" if resp.status_code < 400 else "error")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.TimeoutException:
        _fail_request(req_id, "timeout", "Z.AI upstream timeout", 504)
        return _error_response(504, "timeout", "Z.AI upstream timeout", request)
    except Exception as e:
        _fail_request(req_id, "upstream_error", str(e), 502)
        return _error_response(502, "upstream_error", "Z.AI upstream unavailable", request)


# ═══ Codex (OpenAI GPT) ═══
# Codex routes to a local codex-api-server (CLI passthrough mode) by default,
# or directly to OpenAI's API if CODEX_MODE=api with OPENAI_API_KEY set.

_CODEX_MODE = os.environ.get("CODEX_MODE", "cli").lower()  # cli | api


@router.post("/codex")
async def handle_codex(request: Request):
    """Codex/GPT chat — Anthropic /v1/messages compatible."""
    return await _codex_chat(request)


@router.post("/codex/v1/messages")
async def handle_codex_compat(request: Request):
    """Codex Anthropic-compatible alias."""
    return await _codex_chat(request)


async def _codex_chat(request: Request) -> Response:
    """Forward to local codex-api-server (CLI mode) or OpenAI directly (API mode)."""
    key_id = _authenticate(request)
    body = await request.body()
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return _error_response(400, "invalid_json", "Request body is not valid JSON", request)
    if not payload.get("model"):
        payload["model"] = cfg.get("providers", {}).get("codex", {}).get("default_model", "gpt-5")
        body = json.dumps(payload).encode()

    is_stream = bool(payload.get("stream"))
    rate_err = _check_rate_limit("/codex", payload.get("model", ""))
    if rate_err:
        return _error_response(429, "rate_limited", rate_err, request)

    req_id = _start_request("/codex", key_id, payload.get("model", ""))
    _track_usage("/codex", payload.get("model", ""))

    if _CODEX_MODE == "api":
        if not _OPENAI_KEY:
            _fail_request(req_id, "no_key", "OPENAI_API_KEY not configured", 503)
            return _error_response(503, "codex_unavailable",
                                   "Codex API mode requires OPENAI_API_KEY", request)
        # API mode: translate Anthropic → OpenAI Responses API and forward.
        target_url = "https://api.openai.com/v1/responses"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_OPENAI_KEY}",
        }
        forward_body = json.dumps(_anthropic_to_openai_responses(payload)).encode()
    else:
        # CLI mode: forward to local codex-api-server which wraps `codex exec`.
        target_url = f"{CODEX_TARGET}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
        }
        forward_body = body

    try:
        if is_stream:
            async def codex_stream():
                try:
                    async with _codex_client.stream(
                        "POST", target_url, content=forward_body, headers=headers,
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    _end_request(req_id, "ok")
                except Exception as e:
                    _fail_request(req_id, "stream_error", str(e))
                    raise
            return StreamingResponse(codex_stream(), media_type="text/event-stream")

        resp = await _codex_client.post(target_url, content=forward_body, headers=headers)
        _end_request(req_id, "ok" if resp.status_code < 400 else "error")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )
    except httpx.TimeoutException:
        _fail_request(req_id, "timeout", "Codex upstream timeout", 504)
        return _error_response(504, "timeout", "Codex upstream timeout", request)
    except Exception as e:
        _fail_request(req_id, "upstream_error", str(e), 502)
        return _error_response(502, "upstream_error", "Codex upstream unavailable", request)


def _anthropic_to_openai_responses(payload: dict) -> dict:
    """Translate Anthropic /v1/messages → OpenAI /v1/responses format.
    Used only in CODEX_MODE=api. Best-effort — covers text + tool_result."""
    out_input = []
    system = payload.get("system", "")
    if system:
        if isinstance(system, list):
            system = "\n".join(b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in system)
        out_input.append({"role": "system", "content": system})
    for msg in payload.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content", block.get("output", ""))))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(p for p in parts if p)
        if not content:
            continue
        out_input.append({"role": role, "content": content})
    out = {
        "model": payload.get("model", "gpt-5"),
        "input": out_input,
        "stream": payload.get("stream", False),
    }
    if payload.get("max_tokens"):
        out["max_output_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    return out


# ═══ MiniMax Swarm Status ═══

@router.get("/swarm")
async def swarm_status(request: Request):
    """MiniMax swarm status and recent activity."""
    if not _is_trusted(request):
        _authenticate(request)
    with _swarm_lock:
        recent = list(_swarm_stats["recent"])
        # Compute rolling fail rate from recent swarms only
        recent_workers = sum(r.get("workers", 0) for r in recent)
        recent_successful = sum(r.get("successful", 0) for r in recent)
        recent_failures = recent_workers - recent_successful
        recent_fail_rate = round(recent_failures / recent_workers * 100, 1) if recent_workers > 0 else 0
        return JSONResponse(content={
            "workers_per_swarm": _SWARM_WORKERS,
            "swarm_enabled": _is_swarm_enabled(),
            "total_swarms": _swarm_stats["total_swarms"],
            "active_swarms": _swarm_stats["active_swarms"],
            "total_workers_run": _swarm_stats["total_workers_run"],
            "workers_succeeded": _swarm_stats["workers_succeeded"],
            "worker_failures": _swarm_stats["worker_failures"],
            "consolidations": _swarm_stats["consolidations"],
            "avg_worker_ms": round(_swarm_stats["avg_worker_ms"]),
            "avg_consolidation_ms": round(_swarm_stats["avg_consolidation_ms"]),
            "recent": recent,
            "recent_fail_rate": recent_fail_rate,
        })


@router.post("/swarm/reset")
async def swarm_reset(request: Request):
    """Reset swarm statistics (clears historical failure counts)."""
    if not _is_trusted(request):
        _authenticate(request)
    with _swarm_lock:
        _swarm_stats["total_swarms"] = 0
        _swarm_stats["total_workers_run"] = 0
        _swarm_stats["workers_succeeded"] = 0
        _swarm_stats["worker_failures"] = 0
        _swarm_stats["consolidations"] = 0
        _swarm_stats["avg_worker_ms"] = 0
        _swarm_stats["avg_consolidation_ms"] = 0
        _swarm_stats["recent"].clear()
        # Keep active_swarms as-is (reflects currently running swarms)
    return JSONResponse(content={"status": "ok", "message": "Swarm stats reset"})


@router.post("/swarm/enable")
async def swarm_enable(request: Request):
    """Enable swarm globally."""
    if not _is_trusted(request):
        _authenticate(request)
    _set_swarm_enabled(True)
    return JSONResponse(content={"status": "ok", "swarm_enabled": True})


@router.post("/swarm/disable")
async def swarm_disable(request: Request):
    """Disable swarm globally."""
    if not _is_trusted(request):
        _authenticate(request)
    _set_swarm_enabled(False)
    return JSONResponse(content={"status": "ok", "swarm_enabled": False})


# ═══ Geo / Source Data Endpoint ═══

@router.get("/geo")
async def proxy_geo(request: Request):
    """Return source tracking data for the dashboard map."""
    if not _is_trusted(request):
        _authenticate(request)
    with _geo_lock:
        clean_countries = {}
        for cc, data in _geo_data["by_country"].items():
            clean_countries[cc] = {"count": data.get("count", 0), "last_seen": data.get("last_seen", "")}
        clean_pings = []
        for p in _geo_data["pings"][-100:]:
            clean_pings.append({k: v for k, v in p.items() if k != "key_id"})
        return JSONResponse(content={
            "by_country": clean_countries,
            "pings": clean_pings,
        })


@router.get("/health")
async def proxy_health(request: Request):
    """Local health check — does not depend on the Claude upstream."""
    return JSONResponse(content={
        "status": "ok",
        "service": "agent-router",
        "upstream_claude": "offline" if _upstream_health["claude_api"]["status"] == "down" else "unknown",
    })


@router.get("/network-status")
async def network_status(request: Request):
    """Return Tailscale network status. Requires auth or trusted network."""
    if not _is_trusted(request):
        _authenticate(request)
    result = {
        "transport": "tailscale",
        "hostname": "",
        "ip": "",
        "online": False,
        "peers": 0,
    }
    status = _get_tailscale_status()
    if status:
        self_node = status.get("Self", {})
        result["hostname"] = self_node.get("DNSName", "").rstrip(".")
        ips = self_node.get("TailscaleIPs", [])
        result["ip"] = ips[0] if ips else ""
        result["online"] = self_node.get("Online", False)
        result["peers"] = len(status.get("Peer", {}))

    return JSONResponse(
        content=result,
        headers={"Cache-Control": "no-cache, max-age=0"},
    )



_SOURCE_MAP = {
    "proxy:vps": "remote",
    "dashboard": "dashboard",
    "direct": "local",
    "internal": "local",
}

_MODEL_MAP = {
    "claude-opus-4-6": "opus", "claude-sonnet-4-20250514": "sonnet",
    "claude-sonnet-4-5-20250929": "sonnet", "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
    "MiniMax-M2.5": "minimax", "minimax-m2-5-fallback": "minimax",
}


def _classify_model(model: str) -> str:
    """Classify a model string into a display label."""
    if model in _MODEL_MAP:
        return _MODEL_MAP[model]
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    if "minimax" in m:
        return "minimax"
    return model.split("-")[0] if "-" in model else model


def _sanitize_stats(data: dict) -> dict:
    """Strip identifying metadata from stats before returning to client."""
    out = {
        "uptime_seconds": data.get("uptime_seconds", 0),
        "uptime_human": data.get("uptime_human", ""),
        "requests": data.get("requests", {}),
        "tokens": data.get("tokens", {}),
        "hourly": data.get("hourly", {}),
    }
    # Anonymize models
    by_model = {}
    for model, vals in data.get("by_model", {}).items():
        label = _classify_model(model)
        if label in by_model:
            for k in ("count", "input_tokens", "output_tokens", "total_time"):
                by_model[label][k] = by_model[label].get(k, 0) + vals.get(k, 0)
        else:
            by_model[label] = dict(vals)
        by_model[label].pop("avg_time", None)
    out["by_model"] = by_model

    # Anonymize sources — merge into generic buckets
    by_source = {}
    for src, vals in data.get("by_source", {}).items():
        label = _SOURCE_MAP.get(src, "remote" if src.startswith("proxy:") else "other")
        if label in by_source:
            for k in ("count", "input_tokens", "output_tokens"):
                by_source[label][k] = by_source[label].get(k, 0) + vals.get(k, 0)
        else:
            by_source[label] = {k: vals.get(k, 0) for k in ("count", "input_tokens", "output_tokens")}
    out["by_source"] = by_source

    # Anonymize agents — just count, no identifying names
    out["by_agent"] = {"agents": {"count": sum(v.get("count", 0) for v in data.get("by_agent", {}).values())}}

    # App-level analytics: by_space and by_user (pass through as-is)
    out["by_space"] = data.get("by_space", {})
    out["by_user"] = data.get("by_user", {})

    # Sanitize recent requests
    clean_recent = []
    for r in data.get("recent", []):
        entry = {
            "ts": r.get("ts", ""),
            "endpoint": r.get("endpoint", ""),
            "model": _classify_model(r.get("model", "")),
            "source": _SOURCE_MAP.get(r.get("source", ""), "remote" if r.get("source", "").startswith("proxy:") else "other"),
            "input_tokens": r.get("input_tokens", 0),
            "output_tokens": r.get("output_tokens", 0),
            "elapsed": r.get("elapsed", 0),
            "status": r.get("status", ""),
        }
        if r.get("space_id"):
            entry["space_id"] = r["space_id"]
        if r.get("user_id"):
            entry["user_id"] = r["user_id"]
        clean_recent.append(entry)
    out["recent"] = clean_recent

    # Sanitize errors — strip internal details
    clean_errors = []
    for e in data.get("recent_errors", []):
        clean_errors.append({
            "ts": e.get("ts", ""),
            "endpoint": e.get("endpoint", ""),
            "model": _classify_model(e.get("model", "")),
            "source": _SOURCE_MAP.get(e.get("source", ""), "remote" if e.get("source", "").startswith("proxy:") else "other"),
            "status": e.get("status", ""),
            "error_type": e.get("error_type", "error"),
            "error_detail": "Request failed",  # Generic — never expose internal details
        })
    out["recent_errors"] = clean_errors

    return out


@router.get("/stats")
async def proxy_stats(request: Request):
    """Local stats — built from the router's own usage tracking."""
    if not _is_trusted(request):
        _authenticate(request)

    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")

    with _usage_lock:
        today = _usage_data["daily"].get(day_key, {})
        current_minute = _usage_data["windows"].get(now.strftime("%Y-%m-%d %H:%M"), {})

    total_requests = sum(d.get("requests", 0) for d in today.values())
    total_input = sum(d.get("input_tokens", 0) for d in today.values())
    total_output = sum(d.get("output_tokens", 0) for d in today.values())

    by_model = {}
    for bucket, vals in today.items():
        by_model[bucket] = {
            "count": vals.get("requests", 0),
            "input_tokens": vals.get("input_tokens", 0),
            "output_tokens": vals.get("output_tokens", 0),
        }

    with _request_log_lock:
        recent = list(_recent_requests)[-25:]
        recent_errors = list(_error_log)[-10:]

    return JSONResponse(content={
        "uptime_seconds": int(time.time() - _router_start_time),
        "uptime_human": _humanize_uptime(time.time() - _router_start_time),
        "requests": {"today": total_requests},
        "tokens": {"today": total_input + total_output,
                   "input": total_input, "output": total_output},
        "hourly": {},
        "by_model": by_model,
        "by_source": {},
        "by_agent": {},
        "by_space": {},
        "by_user": {},
        "recent": recent,
        "recent_errors": recent_errors,
    })


# ═══ Usage Limits & Rate Tracking ═══

LIMITS_FILE = os.path.join(_DATA_DIR, "usage-limits.json")

_usage_lock = threading.Lock()
_usage_data = {
    "windows": {},  # "YYYY-MM-DD HH:mm" (per-minute) -> {model: count}
    "daily": {},    # "YYYY-MM-DD" -> {model: {requests, input_tokens, output_tokens}}
    "limits": {
        "claude-opus": {"requests_per_min": 30, "tokens_per_day": 0, "label": "Claude Opus 4.6"},
        "claude-sonnet": {"requests_per_min": 60, "tokens_per_day": 0, "label": "Claude Sonnet"},
        "minimax": {"requests_per_min": 240, "tokens_per_day": 8000000, "label": "MiniMax M2.5 (Swarm 3x)"},
    },
}


def _load_usage():
    global _usage_data
    if os.path.isfile(LIMITS_FILE):
        try:
            with open(LIMITS_FILE) as f:
                saved = json.load(f)
            _usage_data["daily"] = saved.get("daily", {})
            _usage_data["windows"] = saved.get("windows", {})
            if saved.get("limits"):
                _usage_data["limits"].update(saved["limits"])
        except Exception:
            pass


def _save_usage():
    try:
        _atomic_write_json(LIMITS_FILE, _usage_data, indent=None)
    except Exception:
        pass


def _track_usage(endpoint: str, model_hint: str = "", worker_count: int = 1):
    """Track request against rate limits.

    Args:
        endpoint: The API endpoint being called
        model_hint: Optional model name for model-specific limits
        worker_count: Number of workers for swarm calls (default 1)
    """
    if worker_count < 1:
        worker_count = 1

    now = datetime.now(timezone.utc)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    day_key = now.strftime("%Y-%m-%d")
    bucket = _resolve_bucket(endpoint, model_hint)

    with _usage_lock:
        # Per-minute window (multiply by worker_count for swarm)
        if minute_key not in _usage_data["windows"]:
            _usage_data["windows"][minute_key] = {}
        win = _usage_data["windows"][minute_key]
        win[bucket] = win.get(bucket, 0) + worker_count

        # Daily totals (multiply by worker_count for swarm)
        if day_key not in _usage_data["daily"]:
            _usage_data["daily"][day_key] = {}
        day = _usage_data["daily"][day_key]
        if bucket not in day:
            day[bucket] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        day[bucket]["requests"] += worker_count

        # Prune old minute windows (keep last 10 minutes)
        cutoff = (now.timestamp() - 600)
        old_keys = [k for k in _usage_data["windows"]
                    if datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() < cutoff]
        for k in old_keys:
            del _usage_data["windows"][k]

        # Prune old daily (keep 7 days)
        day_keys = sorted(_usage_data["daily"].keys())
        while len(day_keys) > 7:
            del _usage_data["daily"][day_keys.pop(0)]

        # Save periodically
        total = sum(sum(v.values()) for v in _usage_data["windows"].values())
        if total % 10 == 0:
            _save_usage()


def _resolve_bucket(endpoint: str, model_hint: str = "") -> str:
    """Determine the usage bucket for an endpoint/model."""
    ep = endpoint.lower()
    mh = model_hint.lower() if model_hint else ""
    if "/zai" in ep or "glm" in mh:
        return "zai"
    if "/codex" in ep or mh.startswith("gpt") or "codex" in mh:
        return "codex"
    if "opus" in ep or "opus" in mh:
        return "claude-opus"
    if "sonnet" in ep or "sonnet" in mh:
        return "claude-sonnet"
    if "minimax" in ep or "minimax" in mh:
        return "minimax"
    if ep in ("/v1/messages",):
        return "claude-sonnet"
    if ep in ("/suggest",):
        return "minimax"
    return "other"


def _check_rate_limit(endpoint: str, model_hint: str = "", worker_count: int = 1) -> Optional[str]:
    """Check if the current request would exceed rate limits.
    Returns error message if over limit, None if OK.

    Args:
        endpoint: The API endpoint being called
        model_hint: Optional model name for model-specific limits
        worker_count: Number of workers for swarm calls (default 1)
    """
    bucket = _resolve_bucket(endpoint, model_hint)
    limits = _usage_data.get("limits", {}).get(bucket)
    if not limits:
        return None

    now = datetime.now(timezone.utc)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    rpm_limit = limits.get("requests_per_min", 0)

    # For swarm calls, check if we have enough headroom for all workers
    if rpm_limit > 0:
        with _usage_lock:
            window = _usage_data.get("windows", {}).get(minute_key, {})
            current = window.get(bucket, 0)
        # Check if we can accommodate worker_count more requests
        if current + worker_count > rpm_limit:
            return f"Rate limit exceeded for {limits.get('label', bucket)}: {current}/{rpm_limit} requests/min (need {worker_count})"

    tpd_limit = limits.get("tokens_per_day", 0)
    if tpd_limit > 0:
        day_key = now.strftime("%Y-%m-%d")
        with _usage_lock:
            day = _usage_data.get("daily", {}).get(day_key, {}).get(bucket, {})
            total_tokens = day.get("input_tokens", 0) + day.get("output_tokens", 0)
        if total_tokens >= tpd_limit:
            return f"Daily token limit exceeded for {limits.get('label', bucket)}: {total_tokens}/{tpd_limit} tokens"

    return None


def _track_tokens(endpoint: str, model_hint: str, input_tokens: int, output_tokens: int):
    """Add token counts to daily usage tracking."""
    if input_tokens == 0 and output_tokens == 0:
        return
    bucket = _resolve_bucket(endpoint, model_hint)
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _usage_lock:
        if day_key not in _usage_data["daily"]:
            _usage_data["daily"][day_key] = {}
        day = _usage_data["daily"][day_key]
        if bucket not in day:
            day[bucket] = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
        day[bucket]["input_tokens"] += input_tokens
        day[bucket]["output_tokens"] += output_tokens


_load_usage()


@router.get("/limits")
async def proxy_limits(request: Request):
    """Return current usage vs limits."""
    if not _is_trusted(request):
        _authenticate(request)

    now = datetime.now(timezone.utc)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    day_key = now.strftime("%Y-%m-%d")

    with _usage_lock:
        current_minute = _usage_data["windows"].get(minute_key, {})
        today = _usage_data["daily"].get(day_key, {})
        today_all = dict(_usage_data["daily"])  # shallow copy for weekly calc
        limits = _usage_data["limits"]

    result = {}
    for bucket, lim in limits.items():
        rpm_used = current_minute.get(bucket, 0)
        rpm_limit = lim["requests_per_min"]
        day_data = today.get(bucket, {"requests": 0, "input_tokens": 0, "output_tokens": 0})
        day_tokens = day_data.get("input_tokens", 0) + day_data.get("output_tokens", 0)
        token_limit = lim["tokens_per_day"]

        # Weekly totals: sum all days in the current ISO week
        week_tokens = 0
        week_reqs = 0
        week_input = 0
        week_output = 0
        iso_year, iso_week, _ = now.isocalendar()
        for dk, dv in today_all.items():
            try:
                d = datetime.strptime(dk, "%Y-%m-%d")
                dy, dw, _ = d.isocalendar()
                if dy == iso_year and dw == iso_week:
                    bd = dv.get(bucket, {})
                    week_reqs += bd.get("requests", 0)
                    week_input += bd.get("input_tokens", 0)
                    week_output += bd.get("output_tokens", 0)
            except Exception:
                pass
        week_tokens = week_input + week_output

        result[bucket] = {
            "label": lim["label"],
            "rpm": {"used": rpm_used, "limit": rpm_limit, "pct": round(rpm_used / max(rpm_limit, 1) * 100, 1)},
            "daily_requests": day_data.get("requests", 0),
            "daily_tokens": {
                "used": day_tokens, "limit": token_limit,
                "input": day_data.get("input_tokens", 0),
                "output": day_data.get("output_tokens", 0),
                "pct": round(day_tokens / max(token_limit, 1) * 100, 1) if token_limit else 0,
            },
            "weekly": {
                "requests": week_reqs,
                "tokens": week_tokens,
                "input": week_input,
                "output": week_output,
            },
        }

    # Minute resets at next minute boundary
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    # Day resets at midnight UTC
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Weekly resets Monday 4:00 PM AEDT = Monday 05:00 UTC
    days_until_monday = (7 - now.weekday()) % 7  # 0=Mon
    if days_until_monday == 0 and now.hour >= 5:
        days_until_monday = 7  # already past this Monday's reset
    next_weekly = (now + timedelta(days=days_until_monday)).replace(
        hour=5, minute=0, second=0, microsecond=0
    )

    with _provider_lock:
        provider_info = {
            "minimax": {
                "provider_rpm": _provider_limits["minimax"]["rpm"],
                "provider_tpm": _provider_limits["minimax"]["tpm"],
                "remaining_requests": _provider_limits["minimax"]["remaining_requests"],
                "reset_at": _provider_limits["minimax"]["reset_at"],
                "last_seen": _provider_limits["minimax"]["last_updated"],
            },
            "claude": {
                "plan": _provider_limits["claude"]["plan"],
                "window": _provider_limits["claude"]["window"],
                "note": _provider_limits["claude"]["note"],
            },
        }

    return JSONResponse(content={
        "limits": result,
        "providers": provider_info,
        "resets": {
            "rpm_resets_in_sec": int((next_minute - now).total_seconds()),
            "daily_resets_in_sec": int((next_day - now).total_seconds()),
            "weekly_resets_in_sec": int((next_weekly - now).total_seconds()),
            "weekly_resets_at": next_weekly.isoformat(),
        },
        "timestamp": now.isoformat(),
        "day": day_key,
    })


@router.put("/limits")
async def update_limits(request: Request):
    """Update rate limits. Tailscale peers or localhost only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Only available from trusted network")
    body = await request.json()
    with _usage_lock:
        for bucket, vals in body.items():
            if bucket in _usage_data["limits"]:
                _usage_data["limits"][bucket].update(vals)
        _save_usage()
    return {"status": "updated"}


# ═══ Dashboard (password-protected with login page) ═══

DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Login — Proxy Monitor</title>
<style>
:root { --bg: #0a0a0f; --bg2: #12121a; --bg3: #1a1a25; --text: #e0e0e8; --dim: #6a6a7a;
  --border: #2a2a3a; --green: #4ade80; --red: #f87171; --purple: #a78bfa; --cyan: #22d3ee; }
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  display:flex; align-items:center; justify-content:center; min-height:100vh; }
.login-box { background:var(--bg2); border:1px solid var(--border); border-radius:12px;
  padding:40px; width:360px; max-width:90vw; }
h1 { font-size:20px; margin-bottom:4px; }
.sub { color:var(--dim); font-size:12px; margin-bottom:28px; }
label { display:block; font-size:12px; color:var(--dim); margin-bottom:4px; margin-top:16px; }
input { width:100%; padding:10px 12px; background:var(--bg3); border:1px solid var(--border);
  border-radius:6px; color:var(--text); font-size:14px; font-family:inherit; outline:none; }
input:focus { border-color:var(--purple); }
button { width:100%; padding:10px; margin-top:24px; background:var(--purple); color:var(--bg);
  border:none; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit; }
button:hover { opacity:0.9; }
.error { color:var(--red); font-size:12px; margin-top:12px; text-align:center; display:none; }
.error.show { display:block; }
</style></head><body>
<div class="login-box">
  <h1>Proxy Monitor</h1>
  <div class="sub">Sign in to access the dashboard</div>
  <form method="POST" action="login">
    <label>Username</label>
    <input type="text" name="username" autocomplete="username" required autofocus>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit">Sign In</button>
    <div class="error {error_class}">{error_msg}</div>
  </form>
</div></body></html>"""


@router.get("/login")
async def login_page(request: Request):
    """Serve the login page."""
    html = LOGIN_HTML.replace("{error_class}", "").replace("{error_msg}", "")
    return HTMLResponse(content=html)


@router.post("/login")
async def login_submit(request: Request):
    """Validate credentials and set session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    rate_err = _check_login_rate(client_ip)
    if rate_err:
        html = LOGIN_HTML.replace("{error_class}", "show").replace("{error_msg}", rate_err)
        return HTMLResponse(content=html, status_code=429)

    form = await request.form()
    username = str(form.get("username", "")).strip().lower()
    password = str(form.get("password", ""))

    users = _load_users()
    user = users.get(username)
    if user and _verify_password(password, user.get("password_hash", "")):
        _migrate_hash_if_needed(username, password, user["password_hash"])
        _clear_login_attempts(client_ip)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {
            "username": username,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        response = RedirectResponse(url="/api/dashboard", status_code=303)
        response.set_cookie(
            key="session", value=token,
            httponly=True, samesite="lax", max_age=SESSION_EXPIRY,
            secure=not _is_trusted(request),
        )
        return response

    _record_login_failure(client_ip)
    html = LOGIN_HTML.replace("{error_class}", "show").replace("{error_msg}", "Invalid username or password")
    return HTMLResponse(content=html, status_code=401)


@router.post("/auth")
async def api_login(request: Request):
    """JSON login — returns session token for cross-origin dashboard use."""
    client_ip = request.client.host if request.client else "unknown"
    rate_err = _check_login_rate(client_ip)
    if rate_err:
        raise HTTPException(status_code=429, detail=rate_err)

    body = await request.json()
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))

    users = _load_users()
    user = users.get(username)
    if user and _verify_password(password, user.get("password_hash", "")):
        _migrate_hash_if_needed(username, password, user["password_hash"])
        _clear_login_attempts(client_ip)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {
            "username": username,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        response = JSONResponse(content={
            "username": username,
            "role": user.get("role", "viewer"),
        })
        response.set_cookie(
            key="session", value=token,
            httponly=True,
            samesite="lax",
            max_age=SESSION_EXPIRY,
            secure=not _is_trusted(request),
        )
        return response

    _record_login_failure(client_ip)
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    token = request.cookies.get("session")
    if token and token in _sessions:
        del _sessions[token]
    response = RedirectResponse(url="/api/login", status_code=303)
    response.delete_cookie("session")
    return response


@router.get("/dashboard")
async def serve_dashboard(request: Request):
    """Serve the dashboard. Requires login session or trusted network access."""
    if not _is_trusted(request):
        username = _validate_session(request)
        if not username:
            return RedirectResponse(url="/api/login", status_code=303)
    if not os.path.isfile(DASHBOARD_FILE):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    with open(DASHBOARD_FILE) as f:
        html = f.read()
    return HTMLResponse(content=html)


# ═══ API Setup (authenticated — returns the logged-in user's own key) ═══

@router.get("/setup")
async def api_setup(request: Request):
    """Return VPS connection setup. Only admin users see full API keys."""
    auth_id = _authenticate(request)
    role = _get_user_role(auth_id)

    # Get Tailscale address
    endpoint = ""
    status = _get_tailscale_status()
    if status:
        self_node = status.get("Self", {})
        ips = self_node.get("TailscaleIPs", [])
        dns_name = self_node.get("DNSName", "").rstrip(".")
        if dns_name:
            endpoint = f"http://{dns_name}:8001"
        elif ips:
            endpoint = f"http://{ips[0]}:8001"

    if not endpoint:
        try:
            if os.path.isfile(TAILSCALE_CONFIG):
                with open(TAILSCALE_CONFIG) as f:
                    config = json.load(f)
                endpoint = config.get("endpoint", "")
        except Exception:
            pass

    # Find the VPS key — only admin/trusted gets full key
    keys = _load_keys()
    api_key = ""
    key_label = ""
    is_admin = role == "admin" or _is_trusted(request)

    if auth_id.startswith("session:"):
        if "vps" in keys:
            api_key = keys["vps"].get("key", "")
            key_label = keys["vps"].get("label", "VPS")
    else:
        for kid, info in keys.items():
            if info.get("key") == request.headers.get("x-api-key", "") or kid == auth_id:
                api_key = info.get("key", "")
                key_label = info.get("label", kid)
                break

    base_url = (endpoint + "/api") if endpoint else ""
    display_key = api_key if is_admin else _mask_key(api_key)

    # Shell profile setup — only admin gets real setup commands with full keys
    shell_setup = ""
    settings_setup = ""
    test_cmd = ""
    if base_url and api_key and is_admin:
        shell_setup = (
            f'# Add to ~/.bashrc or ~/.zshrc:\n'
            f'export ANTHROPIC_BASE_URL="{base_url}"\n'
            f'export ANTHROPIC_API_KEY="{api_key}"'
        )
        _py = (
            f"import json,os;"
            f"p=os.path.join(os.path.expanduser('~'),'.claude','settings.json');"
            f"os.makedirs(os.path.dirname(p),exist_ok=True);"
            f"d=json.load(open(p)) if os.path.isfile(p) else {{}};"
            f"d.setdefault('env',{{}}).update({{'ANTHROPIC_BASE_URL':'{base_url}','ANTHROPIC_API_KEY':'{api_key}'}});"
            f"json.dump(d,open(p,'w'),indent=2);print('Done.')"
        )
        settings_setup = f'python3 -c "{_py}"'
        test_cmd = (
            f'curl -s -X POST {base_url}/v1/messages '
            f'-H "Content-Type: application/json" '
            f'-H "x-api-key: {api_key}" '
            f'-d \'{{"model":"claude-sonnet-4-20250514","max_tokens":50,"messages":[{{"role":"user","content":"Say ok"}}]}}\''
        )

    return JSONResponse(content={
        "transport": "tailscale",
        "endpoint": endpoint,
        "base_url": base_url,
        "key": display_key,
        "key_label": key_label,
        "shell_setup": shell_setup,
        "settings_setup": settings_setup,
        "test_cmd": test_cmd,
        "note": "Use shell_setup for VPS (survives restarts). settings_setup writes to ~/.claude/settings.json." if is_admin else "Contact admin for full API key.",
    })


# ═══ Key Management (trusted network only) ═══

@router.get("/keys")
async def list_keys(request: Request):
    """List API keys. Admin: full keys. Viewer: masked keys. Remote: own key only."""
    auth_id = _authenticate(request) if not _is_trusted(request) else ""
    role = _get_user_role(auth_id) if auth_id else "admin"
    show_full = (role == "admin") or _is_trusted(request)
    keys = _load_keys()

    if _is_trusted(request) or (auth_id and role == "admin"):
        result = {}
        for key_id, info in keys.items():
            result[key_id] = {
                "label": info.get("label", ""),
                "scopes": info.get("scopes", []),
                "created": info.get("created", ""),
                "key": info.get("key", "") if show_full else _mask_key(info.get("key", "")),
            }
        return {"keys": result}

    # Non-admin / remote: authenticate, show only the user's own key (masked)
    if not auth_id:
        auth_id = _authenticate(request)
        role = _get_user_role(auth_id)
    username = auth_id.split(":", 1)[1] if auth_id.startswith("session:") else auth_id

    result = {}
    if username in keys:
        info = keys[username]
        result[username] = {
            "label": info.get("label", ""),
            "scopes": info.get("scopes", []),
            "created": info.get("created", ""),
            "key": _mask_key(info.get("key", "")),
        }
    return {"keys": result}


@router.post("/keys")
async def create_key(request: Request):
    """Create a new API key. Trusted network only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Key management only available from trusted network")
    body = await request.json()
    label = body.get("label", "unnamed")
    scopes = body.get("scopes", ["models"])
    key_id = body.get("key_id", label.lower().replace(" ", "-"))

    keys = _load_keys()
    if key_id in keys:
        raise HTTPException(status_code=409, detail=f"Key ID '{key_id}' already exists")

    new_key = "pcx-" + key_id + "-" + secrets.token_hex(24)
    keys[key_id] = {
        "key": new_key,
        "label": label,
        "scopes": scopes,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    _save_keys(keys)
    logger.info(f"Created proxy key: {key_id} ({label})")
    return {"key_id": key_id, "key": new_key, "label": label}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str, request: Request):
    """Revoke an API key. Trusted network only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Key management only available from trusted network")
    keys = _load_keys()
    if key_id not in keys:
        raise HTTPException(status_code=404, detail=f"Key '{key_id}' not found")
    del keys[key_id]
    _save_keys(keys)
    logger.info(f"Revoked proxy key: {key_id}")
    return {"status": "revoked", "key_id": key_id}


# ═══ Request Queue ═══

async def _acquire_queue_slot(request: Request, req_id: str = "") -> int:
    """Wait for the upstream Claude semaphore. Returns queue position (0 = immediate).
    Raises HTTPException(503) if queue wait times out or client disconnects."""
    global _queue_depth, _total_queued

    # Fast path: semaphore available
    if _claude_semaphore._value > 0:
        await _claude_semaphore.acquire()
        return 0

    # Slow path: must queue
    async with _queue_lock:
        # Backpressure: reject if queue is too deep
        if _queue_depth >= QUEUE_MAX_DEPTH:
            _queue_depth -= 1  # Don't count this one
            raise HTTPException(
                status_code=503,
                detail=f"Server busy — queue at capacity ({QUEUE_MAX_DEPTH})",
                headers={"Retry-After": "30"},
            )
        _queue_depth += 1
        _total_queued += 1
        position = _queue_depth

    logger.info(f"QUEUE {req_id} position={position} depth={_queue_depth}")

    try:
        deadline = time.time() + QUEUE_WAIT_TIMEOUT
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                await asyncio.wait_for(
                    _claude_semaphore.acquire(),
                    timeout=min(remaining, 5.0),
                )
                return position
            except asyncio.TimeoutError:
                if remaining <= 0:
                    raise
                if await request.is_disconnected():
                    logger.info(f"QUEUE-CANCEL {req_id} client disconnected while waiting")
                    raise HTTPException(status_code=499, detail="Client disconnected while queued")
                continue
    except asyncio.TimeoutError:
        logger.warning(f"QUEUE-TIMEOUT {req_id} waited={QUEUE_WAIT_TIMEOUT}s depth={_queue_depth}")
        raise HTTPException(
            status_code=503,
            detail=f"Queue timeout — {_queue_depth} requests ahead",
            headers={"Retry-After": "30"},
        )
    finally:
        async with _queue_lock:
            _queue_depth = max(0, _queue_depth - 1)


def _release_queue_slot():
    """Release the upstream Claude semaphore."""
    global _total_served
    _total_served += 1
    _claude_semaphore.release()


# ═══ Internal Forwarding ═══

async def _forward(path: str, body: bytes, headers: dict, req_id: str = "") -> JSONResponse:
    """Send request to the model API and return JSON. Uses persistent client."""
    try:
        resp = await _upstream_client.post(path, content=body, headers=headers)
        try:
            data = resp.json()
            # Check for upstream error responses
            if resp.status_code >= 400 and req_id:
                _fail_request(req_id, "upstream_error",
                              data.get("error", str(data))[:300], resp.status_code)
            elif req_id:
                _end_request(req_id, status=str(resp.status_code))
            # Extract and track token usage from response (Anthropic + OpenAI formats)
            usage = data.get("usage") or {}
            if usage:
                inp = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                _track_tokens(path, data.get("model", ""), inp, out)
            return JSONResponse(content=data, status_code=resp.status_code)
        except Exception:
            if req_id:
                _end_request(req_id, status=str(resp.status_code))
            return JSONResponse(content={"raw": resp.text}, status_code=resp.status_code)
    except httpx.ConnectError as e:
        if req_id:
            _fail_request(req_id, "connect_error", str(e), 503)
        raise HTTPException(status_code=503, detail="Claude API server unreachable")
    except httpx.TimeoutException as e:
        if req_id:
            _fail_request(req_id, "timeout", str(e), 504)
        raise HTTPException(status_code=504, detail="Request timed out waiting for upstream")
    except Exception as e:
        if req_id:
            _fail_request(req_id, type(e).__name__, str(e), 500)
        raise HTTPException(status_code=500, detail="Internal proxy error")


async def _forward_with_retry(path: str, body: bytes, headers: dict, req_id: str = "") -> JSONResponse:
    """Forward with retry on transient errors. Only for non-streaming requests."""
    last_exc = None
    for attempt in range(1 + len(RETRY_DELAYS)):
        try:
            resp = await _forward(path, body, headers, req_id=req_id if attempt == 0 else "")
            if resp.status_code in RETRYABLE_CODES and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.info(f"RETRY {req_id} attempt={attempt+1} status={resp.status_code} delay={delay}s")
                await asyncio.sleep(delay)
                continue
            return resp
        except HTTPException as e:
            if e.status_code in RETRYABLE_CODES and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.info(f"RETRY {req_id} attempt={attempt+1} status={e.status_code} delay={delay}s")
                await asyncio.sleep(delay)
                last_exc = e
                continue
            raise
        except Exception as e:
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.info(f"RETRY {req_id} attempt={attempt+1} error={type(e).__name__} delay={delay}s")
                await asyncio.sleep(delay)
                last_exc = e
                continue
            raise
    if last_exc:
        raise last_exc


async def _stream_forward(path: str, body: bytes, headers: dict, req_id: str = "",
                          request: Request = None, on_complete=None,
                          cascade_payload: dict = None) -> StreamingResponse:
    """Stream SSE from upstream. Uses dedicated client per stream (required for httpx streaming).
    If cascade_payload is provided and upstream returns 429, cascades to MiniMax."""
    # Streaming requires a dedicated client — httpx streams hold the connection open
    stream_client = httpx.AsyncClient(base_url=PROXY_TARGET, timeout=_UPSTREAM_TIMEOUT)
    bytes_sent = 0
    chunks_sent = 0
    first_byte_time = None
    start = time.time()
    stream_input_tokens = 0
    stream_output_tokens = 0
    stream_model = ""

    async def stream_generator():
        nonlocal bytes_sent, chunks_sent, first_byte_time
        nonlocal stream_input_tokens, stream_output_tokens, stream_model
        try:
            async with stream_client.stream("POST", path, content=body, headers=headers) as resp:
                if resp.status_code == 429 and cascade_payload:
                    # Read and discard error body
                    async for _ in resp.aiter_bytes():
                        pass
                    if req_id:
                        _fail_request(req_id, "upstream_429", "Rate limited, cascading to MiniMax", 429)
                    # Cascade to MiniMax streaming
                    cascade_resp = await _cascade_to_minimax(
                        cascade_payload, req_id=req_id, is_stream=True,
                        request=request, on_complete=on_complete,
                    )
                    if cascade_resp and hasattr(cascade_resp, 'body_iterator'):
                        async for chunk in cascade_resp.body_iterator:
                            yield chunk
                        return
                    # MiniMax also failed — yield error
                    yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'rate_limit', 'message': 'Rate limited on all providers'}})}\n\n".encode()
                    return
                if resp.status_code >= 400:
                    # Read error body and forward it
                    error_body = b""
                    async for chunk in resp.aiter_bytes():
                        error_body += chunk
                    if req_id:
                        _fail_request(req_id, "upstream_error",
                                      error_body.decode("utf-8", errors="replace")[:300],
                                      resp.status_code)
                    yield error_body
                    return

                async for chunk in resp.aiter_bytes():
                    # Check client disconnect every 10 chunks
                    if request and chunks_sent > 0 and chunks_sent % 10 == 0:
                        if await request.is_disconnected():
                            logger.info(f"STREAM-CANCEL {req_id} client disconnected after {chunks_sent} chunks")
                            if req_id:
                                _end_request(req_id, status="client_disconnect")
                            return
                    if first_byte_time is None:
                        first_byte_time = time.time()
                    bytes_sent += len(chunk)
                    chunks_sent += 1
                    # Extract token usage from SSE events
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        for line in text.split("\n"):
                            if line.startswith("data: "):
                                d = json.loads(line[6:])
                                if d.get("type") == "message_start":
                                    msg = d.get("message", {})
                                    stream_model = msg.get("model", "")
                                    u = msg.get("usage", {})
                                    stream_input_tokens += u.get("input_tokens", 0)
                                elif d.get("type") == "message_delta":
                                    u = d.get("usage", {})
                                    stream_output_tokens += u.get("output_tokens", 0)
                    except Exception:
                        pass
                    yield chunk

            # Stream completed successfully
            elapsed = time.time() - start
            ttfb = (first_byte_time - start) if first_byte_time else elapsed
            if req_id:
                _end_request(req_id, status="ok")
            # Track accumulated token usage
            _track_tokens(path, stream_model, stream_input_tokens, stream_output_tokens)
            if elapsed > 30 or ttfb > 30:
                logger.info(
                    f"STREAM {path} elapsed={elapsed:.0f}s ttfb={ttfb:.0f}s "
                    f"chunks={chunks_sent} bytes={bytes_sent}"
                )

        except httpx.ConnectError as e:
            if req_id:
                _fail_request(req_id, "connect_error", str(e), 503)
            yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'proxy_error', 'message': 'Claude API server unreachable'}})}\n\n".encode()
        except httpx.ReadError as e:
            if req_id:
                _fail_request(req_id, "stream_read_error", str(e), 502)
            yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'proxy_error', 'message': 'Stream interrupted — upstream connection lost'}})}\n\n".encode()
        except Exception as e:
            if req_id:
                _fail_request(req_id, type(e).__name__, str(e), 500)
            yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'proxy_error', 'message': f'Stream error: {type(e).__name__}'}})}\n\n".encode()
        finally:
            await stream_client.aclose()
            if on_complete:
                on_complete()

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══ Monitor Endpoint ═══

_process_cache = {"data": {}, "ts": 0}

def _get_process_status() -> dict:
    """Check if proxy and API processes are running. Cached for 30s."""
    now = time.time()
    if (now - _process_cache["ts"]) < 30 and _process_cache["data"]:
        return _process_cache["data"]
    result = {}
    for name, port in [("proxy", 8001), ("claude_api", 8000)]:
        try:
            out = subprocess.run(["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-P", "-t"],
                          capture_output=True, text=True, timeout=5)
            pids = out.stdout.strip().split("\n") if out.stdout.strip() else []
            result[name] = {"running": len(pids) > 0, "pids": pids}
        except Exception:
            result[name] = {"running": False}
    _process_cache["data"] = result
    _process_cache["ts"] = now
    return result


@router.get("/monitor")
async def proxy_monitor(request: Request):
    """Run service health checks and return a diagnostic report. Trusted network only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Only available from trusted network")

    report = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # Proxy health
    try:
        r = await _upstream_client.get("/health")
        data = r.json()
        up = data.get("status") in ("healthy", "ok")
        report["proxy"] = {"status": "UP" if up else "DEGRADED", **data}
    except Exception as e:
        report["proxy"] = {"status": "DOWN", "error": str(e)}

    # Active requests and recent errors
    with _request_log_lock:
        report["active_requests"] = len(_active_requests)
        report["recent_errors"] = len(_error_log)
        report["last_errors"] = list(_error_log)[-5:]

    # Tailscale
    ts = _get_tailscale_status()
    if ts:
        self_node = ts.get("Self", {})
        report["tailscale"] = {
            "status": "ONLINE",
            "hostname": self_node.get("DNSName", "").rstrip("."),
            "ip": (self_node.get("TailscaleIPs", []) or [""])[0],
            "peers": len(ts.get("Peer", {})),
        }
    else:
        report["tailscale"] = {"status": "OFFLINE"}

    # API keys stability
    keys = _load_keys()
    key_summary = []
    for kid, info in keys.items():
        kv = info.get("key", "")
        key_summary.append({"id": kid, "prefix": kv[:12] + "..." if len(kv) > 12 else kv, "created": info.get("created", "")})
    report["keys"] = {
        "count": len(keys),
        "mtime": "",
        "keys": key_summary,
    }
    try:
        report["keys"]["mtime"] = datetime.fromtimestamp(os.path.getmtime(KEYS_FILE)).isoformat(timespec="seconds")
    except Exception:
        pass

    # Processes (cached for 30s — lsof is expensive)
    report["processes"] = _get_process_status()

    # Upstream health
    report["upstreams"] = {k: {**v} for k, v in _upstream_health.items()}

    # Queue status
    report["queue"] = {
        "depth": _queue_depth,
        "total_queued": _total_queued,
        "total_served": _total_served,
        "busy": _claude_semaphore._value == 0,
    }

    # Issues
    issues = []
    if report["proxy"].get("status") not in ("UP", "healthy", "ok"):
        issues.append("Proxy is DOWN")
    if report["tailscale"].get("status") != "ONLINE":
        issues.append("Tailscale is OFFLINE")
    err_rate = report["proxy"].get("error_rate", 0)
    if err_rate > 10:
        issues.append(f"High error rate: {err_rate}%")
    if _queue_depth > 5:
        issues.append(f"Queue depth high: {_queue_depth}")

    report["health"] = "HEALTHY" if not issues else "DEGRADED"
    report["issues"] = issues

    return JSONResponse(content=report)


@router.get("/errors")
async def proxy_errors(request: Request):
    """Recent errors with full detail. Trusted network only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Only available from trusted network")
    with _request_log_lock:
        errors = list(_error_log)
        active = {k: {**v, "elapsed": round(time.time() - v["started"], 1)}
                  for k, v in _active_requests.items()}
    return JSONResponse(content={
        "recent_errors": errors,
        "active_requests": active,
        "error_count": len(errors),
        "active_count": len(active),
    })


@router.get("/active")
async def proxy_active(request: Request):
    """Currently in-flight requests. Trusted network only."""
    if not _is_trusted(request):
        raise HTTPException(status_code=403, detail="Only available from trusted network")
    with _request_log_lock:
        # Prune requests stale for >10 minutes (leaked/stuck)
        now = time.time()
        stale_keys = [k for k, v in _active_requests.items() if (now - v["started"]) > 600]
        for k in stale_keys:
            _active_requests.pop(k, None)

        active = {}
        for k, v in _active_requests.items():
            elapsed = round(now - v["started"], 1)
            active[k] = {
                "endpoint": v.get("endpoint", ""),
                "model": v.get("model", ""),
                "elapsed": elapsed,
                "started_at": v.get("started_at", ""),
                "key_id": v.get("key_id", ""),
                "stream": v.get("stream", False),
                "stale": elapsed > 300,
            }
    # Also check upstream Claude API state
    upstream_state = {}
    try:
        resp = await _upstream_client.get("/claude-state", timeout=2.0)
        if resp.status_code == 200:
            upstream_state = resp.json()
    except Exception:
        pass

    return JSONResponse(content={
        "active": active,
        "count": len(active),
        "queue": {
            "depth": _queue_depth,
            "total_queued": _total_queued,
            "total_served": _total_served,
            "busy": _claude_semaphore._value == 0,
        },
        "upstream": upstream_state,
    })


# ═══ Unified Dashboard Summary ═══
# Returns all dashboard data in a single response, reducing 7 fetches to 1.
# Each section is independently try/caught so one failure doesn't break the whole response.

_summary_client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{cfg['services']['proxy']['port']}/api", timeout=httpx.Timeout(timeout=cfg_timeout("internal_api"), connect=5.0))

@router.get("/dashboard/summary")
async def dashboard_summary(request: Request):
    """All dashboard data in one response. Reduces polling overhead by ~85%."""
    if not _is_trusted(request):
        _authenticate(request)

    # Forward auth headers for internal calls
    hdrs = {}
    for k in ("x-api-key", "cookie", "authorization"):
        v = request.headers.get(k)
        if v:
            hdrs[k] = v
    # Mark as trusted for internal calls (localhost to localhost)
    hdrs["x-forwarded-for"] = "127.0.0.1"

    endpoints = {
        "stats": "/stats",
        "geo": "/geo",
        "limits": "/limits",
        "network": "/network-status",
        "monitor": "/monitor",
        "active": "/active",
        "swarm": "/swarm",
    }

    async def fetch_section(key, path):
        try:
            resp = await _summary_client.get(path, headers=hdrs)
            if resp.status_code == 200:
                return key, resp.json()
            return key, {"_error": f"HTTP {resp.status_code}"}
        except Exception as e:
            return key, {"_error": str(e)}

    results = await asyncio.gather(*[fetch_section(k, p) for k, p in endpoints.items()])
    summary = {k: v for k, v in results}
    
    # Inject live values the dashboard JS expects
    keys = _load_keys()
    with _request_log_lock:
        active_count = len(_active_requests)
    summary["meta"] = {
        "key_count": len(keys),
        "active_count": active_count,
        "queue_depth": _queue_depth,
    }
    return JSONResponse(content=summary)


# ── Platform Health Cascade ──

_health_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout=cfg_timeout("health_check"), connect=3.0))

@router.get("/platform-health")
async def platform_health(request: Request):
    """Check all platform services in parallel. Returns per-service status."""
    if not _is_trusted(request):
        _authenticate(request)

    from platform_config import all_services

    async def check_service(name, svc):
        health_url = svc.get("health")
        if not health_url:
            return name, {"status": "no_health_endpoint", "port": svc.get("port")}
        start = time.time()
        try:
            resp = await _health_client.get(health_url)
            elapsed_ms = round((time.time() - start) * 1000)
            if resp.status_code == 200:
                body = {}
                try:
                    body = resp.json()
                except Exception:
                    pass
                return name, {
                    "status": "healthy",
                    "latency_ms": elapsed_ms,
                    "port": svc.get("port"),
                    "details": body,
                }
            return name, {
                "status": "unhealthy",
                "http_status": resp.status_code,
                "latency_ms": elapsed_ms,
                "port": svc.get("port"),
            }
        except httpx.ConnectError:
            return name, {"status": "offline", "port": svc.get("port")}
        except httpx.TimeoutException:
            elapsed_ms = round((time.time() - start) * 1000)
            return name, {"status": "timeout", "latency_ms": elapsed_ms, "port": svc.get("port")}
        except Exception as e:
            return name, {"status": "error", "error": str(e), "port": svc.get("port")}

    services = all_services()
    results = await asyncio.gather(*[check_service(n, s) for n, s in services])
    service_map = {k: v for k, v in results}

    healthy_count = sum(1 for v in service_map.values() if v["status"] == "healthy")
    total_count = len(service_map)

    return JSONResponse(content={
        "platform": "healthy" if healthy_count == total_count else "degraded" if healthy_count > 0 else "down",
        "healthy": healthy_count,
        "total": total_count,
        "services": service_map,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
