"""
Agent Router — platform config loader.

Single source of truth for service configuration. Reads platform.json
once at import, validates required fields, exposes typed accessors.

Usage:
    from platform_config import cfg, service_url, timeout

    url = service_url("claude-api")        # "http://127.0.0.1:8000"
    t = timeout("claude_opus")             # 180.0
    port = cfg["services"]["proxy"]["port"]  # 8001
"""

import json
import os
import sys
import uuid

# ── Locate platform.json ──
# Search order: AGENT_ROUTER_CONFIG env var → same dir as this file → ~/.agent-router/platform.json
_SEARCH_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.json"),
    os.path.expanduser("~/.agent-router/platform.json"),
]
if os.environ.get("AGENT_ROUTER_CONFIG"):
    _SEARCH_PATHS.insert(0, os.environ["AGENT_ROUTER_CONFIG"])

_config_path = None
for _p in _SEARCH_PATHS:
    if os.path.isfile(_p):
        _config_path = _p
        break

if not _config_path:
    print(
        "[agent-router] FATAL: platform.json not found. Searched:\n  "
        + "\n  ".join(_SEARCH_PATHS)
        + "\n  Set AGENT_ROUTER_CONFIG=/path/to/platform.json to override.",
        file=sys.stderr,
    )
    sys.exit(1)

with open(_config_path) as _f:
    cfg = json.load(_f)

# ── Validation ──
_REQUIRED_KEYS = ["services", "upstream", "timeouts"]
_missing = [k for k in _REQUIRED_KEYS if k not in cfg]
if _missing:
    print(
        f"[agent-router] FATAL: platform.json missing required keys: {_missing}",
        file=sys.stderr,
    )
    sys.exit(1)

_REQUIRED_SERVICES = ["claude-api", "proxy"]
_missing_svc = [s for s in _REQUIRED_SERVICES if s not in cfg["services"]]
if _missing_svc:
    print(
        f"[agent-router] FATAL: platform.json missing required services: {_missing_svc}",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Accessors ──

def service_url(name: str) -> str:
    """Base URL for a named service (e.g. 'claude-api' -> 'http://127.0.0.1:8000')."""
    svc = cfg["services"].get(name)
    if not svc:
        raise KeyError(f"Unknown service: {name}")
    return f"http://{svc['host']}:{svc['port']}"


def service_health_url(name: str) -> str:
    svc = cfg["services"].get(name)
    if not svc:
        raise KeyError(f"Unknown service: {name}")
    return svc.get("health", f"http://{svc['host']}:{svc['port']}/health")


def service_port(name: str) -> int:
    svc = cfg["services"].get(name)
    if not svc:
        raise KeyError(f"Unknown service: {name}")
    return svc["port"]


def service_label(name: str) -> str:
    svc = cfg["services"].get(name)
    if not svc:
        raise KeyError(f"Unknown service: {name}")
    return svc.get("label", "")


def service_plist(name: str) -> str:
    svc = cfg["services"].get(name)
    if not svc or not svc.get("plist"):
        raise KeyError(f"No plist for service: {name}")
    la_dir = os.path.expanduser(cfg.get("paths", {}).get("launchagents_dir", "~/Library/LaunchAgents"))
    return os.path.join(la_dir, svc["plist"])


def timeout(name: str) -> float:
    t = cfg.get("timeouts", {}).get(name)
    if t is None:
        raise KeyError(f"Unknown timeout: {name}")
    return float(t)


def upstream(name: str) -> str:
    u = cfg.get("upstream", {}).get(name)
    if not u:
        raise KeyError(f"Unknown upstream: {name}")
    return u


def expand_path(name: str) -> str:
    p = cfg.get("paths", {}).get(name)
    if not p:
        raise KeyError(f"Unknown path: {name}")
    return os.path.expanduser(p)


def swarm_enabled() -> bool:
    return cfg.get("swarm", {}).get("enabled", True)


def swarm_workers() -> int:
    return cfg.get("swarm", {}).get("workers", 3)


def cascade_chain() -> list:
    """Ordered list of providers to fall back to on rate-limit/error."""
    return cfg.get("cascade", {}).get("chain", [])


def cascade_enabled() -> bool:
    return cfg.get("cascade", {}).get("enabled", True)


def provider_default_model(provider: str) -> str:
    return cfg.get("providers", {}).get(provider, {}).get("default_model", "")


def all_services():
    return cfg["services"].items()


def generate_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


_svc_count = len(cfg["services"])
_version = cfg.get("_version", "?")
print(f"[agent-router] Loaded platform.json v{_version} ({_svc_count} services) from {_config_path}")
