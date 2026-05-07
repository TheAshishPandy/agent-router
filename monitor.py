#!/usr/bin/env python3
"""
Agent Router Monitor — Service health, key stability, and diagnostics.

Run:  python3 monitor.py              # Full report
      python3 monitor.py --json       # Machine-readable JSON
      python3 monitor.py --watch      # Continuous monitoring (30s interval)
      python3 monitor.py --brief      # One-line summary

Checks:
  - Proxy health (port 8001)
  - Upstream Claude API health (port 8000)
  - Tailscale connectivity and peer count
  - API key stability (detects file modifications, key count changes)
  - Request success/error/timeout rates
  - Response time analysis (slow requests, timeouts)
  - Per-peer breakdown (local vs remote nodes)
  - Rate limit headroom
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

PROXY = "http://127.0.0.1:8001"
UPSTREAM = "http://127.0.0.1:8000"
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KEYS_FILE = os.path.join(_DATA_DIR, "proxy-keys.json")
SNAPSHOT_FILE = os.path.join(_DATA_DIR, "monitor-snapshot.json")

# ANSI colors
G = "\033[32m"    # green
Y = "\033[33m"    # yellow
R = "\033[31m"    # red
C = "\033[36m"    # cyan
D = "\033[90m"    # dim
B = "\033[1m"     # bold
N = "\033[0m"     # reset


def _fetch(url, timeout=5):
    """Fetch JSON from URL. Returns (data, error)."""
    import urllib.request
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, str(e)


def _file_hash(path):
    """SHA256 of file contents."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""


def _file_mtime(path):
    """Last modified time as ISO string."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    except Exception:
        return ""


def _load_snapshot():
    """Load previous monitor snapshot for diff comparison."""
    if os.path.isfile(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_snapshot(snap):
    """Save current snapshot for next comparison."""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f, indent=2)
    except Exception:
        pass


def check_proxy_health():
    """Check if the proxy is responding."""
    data, err = _fetch(f"{PROXY}/api/health")
    if err:
        return {"status": "DOWN", "error": err}
    status_val = data.get("status", "")
    up = status_val in ("healthy", "ok") or data.get("claude_cli") == "ok"
    return {
        "status": "UP" if up else "DEGRADED",
        "uptime": data.get("uptime", 0),
        "total_requests": data.get("total_requests", 0),
        "error_rate": data.get("error_rate", 0),
        "claude_cli": data.get("claude_cli", "unknown"),
    }


def check_upstream_health():
    """Check if upstream Claude API (port 8000) is responding."""
    data, err = _fetch(f"{UPSTREAM}/health")
    if err:
        return {"status": "DOWN", "error": err}
    return {
        "status": "UP" if data.get("status") == "healthy" else "DEGRADED",
        "uptime": data.get("uptime", 0),
    }


def check_tailscale():
    """Check Tailscale status."""
    data, err = _fetch(f"{PROXY}/api/network-status")
    if err:
        return {"status": "UNKNOWN", "error": err}
    return {
        "status": "ONLINE" if data.get("online") else "OFFLINE",
        "hostname": data.get("hostname", ""),
        "ip": data.get("ip", ""),
        "peers": data.get("peers", 0),
    }


def check_keys():
    """Check API key file stability."""
    result = {
        "file_exists": os.path.isfile(KEYS_FILE),
        "file_hash": _file_hash(KEYS_FILE),
        "file_mtime": _file_mtime(KEYS_FILE),
        "key_count": 0,
        "keys": [],
        "changed": False,
    }

    if result["file_exists"]:
        try:
            with open(KEYS_FILE) as f:
                data = json.load(f)
            keys = data.get("keys", {})
            result["key_count"] = len(keys)
            for kid, info in keys.items():
                key_val = info.get("key", "")
                result["keys"].append({
                    "id": kid,
                    "label": info.get("label", ""),
                    "prefix": key_val[:12] + "..." if len(key_val) > 12 else key_val,
                    "created": info.get("created", ""),
                })
        except Exception as e:
            result["error"] = str(e)

    # Compare with previous snapshot
    prev = _load_snapshot()
    prev_hash = prev.get("keys_hash", "")
    if prev_hash and prev_hash != result["file_hash"]:
        result["changed"] = True
        result["change_detail"] = f"Hash changed: {prev_hash[:12]}... -> {result['file_hash'][:12]}..."

    return result


def check_stats():
    """Get request stats and analyze health patterns."""
    data, err = _fetch(f"{PROXY}/api/stats")
    if err:
        return {"status": "UNAVAILABLE", "error": err}

    requests = data.get("requests", {})
    tokens = data.get("tokens", {})
    recent = data.get("recent", [])
    errors = data.get("recent_errors", [])

    total = requests.get("total", 0)
    success = requests.get("success", 0)
    errs = requests.get("errors", 0)
    timeouts = requests.get("timeouts", 0)

    # Analyze response times from recent requests
    times = [r.get("elapsed", 0) for r in recent if r.get("elapsed")]
    slow_requests = [r for r in recent if r.get("elapsed", 0) > 60]

    result = {
        "total_requests": total,
        "success": success,
        "errors": errs,
        "timeouts": timeouts,
        "error_rate": round(errs / max(total, 1) * 100, 1),
        "timeout_rate": round(timeouts / max(total, 1) * 100, 1),
        "tokens_in": tokens.get("total_input", 0),
        "tokens_out": tokens.get("total_output", 0),
        "recent_count": len(recent),
        "recent_errors_count": len(errors),
        "slow_requests": len(slow_requests),
    }

    if times:
        result["avg_response_time"] = round(sum(times) / len(times), 1)
        result["max_response_time"] = round(max(times), 1)
        result["p90_response_time"] = round(sorted(times)[int(len(times) * 0.9)], 1)

    # By model breakdown
    by_model = data.get("by_model", {})
    result["by_model"] = {}
    for model, vals in by_model.items():
        result["by_model"][model] = {
            "count": vals.get("count", 0),
            "input": vals.get("input_tokens", 0),
            "output": vals.get("output_tokens", 0),
        }

    # Recent errors detail
    if errors:
        result["last_errors"] = errors[-5:]

    return result


def check_limits():
    """Check rate limit headroom."""
    data, err = _fetch(f"{PROXY}/api/limits")
    if err:
        return {"status": "UNAVAILABLE", "error": err}

    limits = data.get("limits", {})
    result = {}
    for bucket, info in limits.items():
        rpm = info.get("rpm", {})
        daily = info.get("daily_tokens", {})
        result[bucket] = {
            "label": info.get("label", bucket),
            "rpm_used": rpm.get("used", 0),
            "rpm_limit": rpm.get("limit", 0),
            "rpm_pct": rpm.get("pct", 0),
            "daily_requests": info.get("daily_requests", 0),
            "daily_tokens_used": daily.get("used", 0),
            "daily_tokens_limit": daily.get("limit", 0),
        }
    return result


def check_processes():
    """Check if the expected processes are running, and detect zombie claude processes."""
    result = {}
    checks = {
        "proxy": ("agent-router.proxy", 8001),
        "claude_api": ("agent-router.claude-api", 8000),
        "codex_api": ("agent-router.codex-api", 8006),
    }
    for name, (label, port) in checks.items():
        try:
            out = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-P", "-t"],
                capture_output=True, text=True, timeout=5,
            )
            pids = out.stdout.strip().split("\n") if out.stdout.strip() else []
            result[name] = {
                "running": len(pids) > 0,
                "pids": pids,
                "port": port,
            }
        except Exception as e:
            result[name] = {"running": False, "error": str(e)}

    # Detect zombie/stale claude processes
    try:
        out = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        claude_procs = []
        for line in out.stdout.strip().split("\n"):
            if "claude" in line.lower() and "grep" not in line:
                parts = line.split()
                if len(parts) >= 11:
                    claude_procs.append({
                        "pid": parts[1],
                        "cpu": parts[2],
                        "mem": parts[3],
                        "started": parts[8] if len(parts) > 8 else "",
                        "cmd": " ".join(parts[10:])[:80],
                    })
        result["claude_processes"] = claude_procs
        result["claude_process_count"] = len(claude_procs)
    except Exception:
        result["claude_processes"] = []

    return result


def check_active_requests():
    """Check in-flight requests through the proxy."""
    data, err = _fetch(f"{PROXY}/api/active")
    if err:
        return {"status": "UNAVAILABLE", "error": err}
    active = data.get("active", {})
    stale = {k: v for k, v in active.items() if v.get("stale")}
    return {
        "count": len(active),
        "stale_count": len(stale),
        "requests": active,
        "stale": stale,
    }


def check_errors():
    """Check recent proxy errors."""
    data, err = _fetch(f"{PROXY}/api/errors")
    if err:
        return {"status": "UNAVAILABLE", "error": err}
    return {
        "count": data.get("error_count", 0),
        "recent": data.get("recent_errors", [])[-10:],
    }


def check_geo():
    """Check source tracking / peer activity."""
    data, err = _fetch(f"{PROXY}/api/geo")
    if err:
        return {"status": "UNAVAILABLE", "error": err}

    by_country = data.get("by_country", {})
    pings = data.get("pings", [])

    result = {
        "active_nodes": len(by_country),
        "total_pings": sum(c.get("count", 0) for c in by_country.values()),
        "nodes": {},
    }
    for node, info in by_country.items():
        result["nodes"][node] = {
            "count": info.get("count", 0),
            "last_seen": info.get("last_seen", ""),
        }

    # Recent activity (last 10 pings)
    if pings:
        result["recent_pings"] = pings[-10:]

    return result


def generate_report(as_json=False):
    """Run all checks and generate a report."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    report = {
        "timestamp": now,
        "proxy": check_proxy_health(),
        "upstream": check_upstream_health(),
        "tailscale": check_tailscale(),
        "keys": check_keys(),
        "stats": check_stats(),
        "limits": check_limits(),
        "processes": check_processes(),
        "active": check_active_requests(),
        "errors": check_errors(),
        "geo": check_geo(),
    }

    # Compute overall health
    issues = []
    if report["proxy"].get("status") != "UP":
        issues.append("Proxy is DOWN")
    if report["upstream"].get("status") != "UP":
        issues.append("Upstream Claude API is DOWN")
    if report["tailscale"].get("status") != "ONLINE":
        issues.append("Tailscale is OFFLINE")
    if report["keys"].get("changed"):
        issues.append("API keys file CHANGED since last check")
    if not report["keys"].get("file_exists"):
        issues.append("API keys file MISSING")
    error_rate = report["stats"].get("error_rate", 0)
    if error_rate > 10:
        issues.append(f"High error rate: {error_rate}%")
    timeout_rate = report["stats"].get("timeout_rate", 0)
    if timeout_rate > 5:
        issues.append(f"High timeout rate: {timeout_rate}%")
    stale = report["active"].get("stale_count", 0)
    if stale > 0:
        issues.append(f"{stale} stale request(s) (>5 min)")
    zombie_count = report["processes"].get("claude_process_count", 0)
    if zombie_count > 5:
        issues.append(f"{zombie_count} claude processes running (possible zombies)")

    report["health"] = "HEALTHY" if not issues else "DEGRADED"
    report["issues"] = issues

    # Save snapshot for next diff
    _save_snapshot({
        "keys_hash": report["keys"].get("file_hash", ""),
        "keys_count": report["keys"].get("key_count", 0),
        "timestamp": now,
        "total_requests": report["stats"].get("total_requests", 0),
    })

    if as_json:
        return report

    return report


def format_report(report):
    """Format report as human-readable text."""
    lines = []
    now = report["timestamp"]

    # Header
    health = report["health"]
    hcolor = G if health == "HEALTHY" else R
    lines.append(f"\n{B}{'='*60}{N}")
    lines.append(f"{B}  Agent Router Monitor{N}")
    lines.append(f"{D}  {now}{N}")
    lines.append(f"  Status: {hcolor}{B}{health}{N}")
    lines.append(f"{B}{'='*60}{N}")

    # Issues
    if report["issues"]:
        lines.append(f"\n{R}{B}  ISSUES:{N}")
        for issue in report["issues"]:
            lines.append(f"  {R}!{N} {issue}")

    # Services
    lines.append(f"\n{B}  Services{N}")
    proxy = report["proxy"]
    pstatus = G + "UP" + N if proxy.get("status") == "UP" else R + proxy.get("status", "?") + N
    lines.append(f"  Proxy (8001):      {pstatus}  uptime={_fmt_duration(proxy.get('uptime', 0))}  reqs={proxy.get('total_requests', 0)}  err={proxy.get('error_rate', 0)}%")

    upstream = report["upstream"]
    ustatus = G + "UP" + N if upstream.get("status") == "UP" else R + upstream.get("status", "?") + N
    lines.append(f"  Claude API (8000): {ustatus}  uptime={_fmt_duration(upstream.get('uptime', 0))}")

    ts = report["tailscale"]
    tstatus = G + "ONLINE" + N if ts.get("status") == "ONLINE" else R + ts.get("status", "?") + N
    lines.append(f"  Tailscale:         {tstatus}  peers={ts.get('peers', 0)}  host={ts.get('hostname', '?')}")

    # Processes
    procs = report["processes"]
    for name in ("proxy", "claude_api"):
        info = procs.get(name, {})
        if isinstance(info, dict) and "running" in info:
            running = info.get("running", False)
            pcolor = G if running else R
            pids = ",".join(info.get("pids", []))
            lines.append(f"  {name}: {pcolor}{'running' if running else 'STOPPED'}{N}  port={info.get('port', '?')}  pids={pids}")

    claude_count = procs.get("claude_process_count", 0)
    if claude_count > 0:
        ccolor = Y if claude_count > 3 else R if claude_count > 5 else D
        lines.append(f"  claude processes:   {ccolor}{claude_count}{N}")
        for p in procs.get("claude_processes", []):
            cpu = float(p.get("cpu", 0))
            cpc = R if cpu > 50 else Y if cpu > 10 else D
            lines.append(f"    PID {p['pid']}  cpu={cpc}{p['cpu']}%{N}  mem={p['mem']}%  {D}{p.get('cmd', '')[:60]}{N}")

    # Active requests
    active = report.get("active", {})
    active_count = active.get("count", 0)
    stale_count = active.get("stale_count", 0)
    if active_count > 0:
        lines.append(f"\n{B}  In-Flight Requests{N}  ({active_count} active, {R}{stale_count} stale{N})" if stale_count else f"\n{B}  In-Flight Requests{N}  ({active_count} active)")
        for rid, req in active.get("requests", {}).items():
            elapsed = req.get("elapsed", 0)
            ecolor = R if elapsed > 300 else Y if elapsed > 60 else G
            lines.append(f"  {rid}: {req.get('endpoint', '?')}  model={req.get('model', '?')}  {ecolor}{elapsed:.0f}s{N}  stream={req.get('stream', False)}")

    # API Keys
    lines.append(f"\n{B}  API Keys{N}")
    keys = report["keys"]
    kstatus = G + "STABLE" + N if not keys.get("changed") else R + "CHANGED" + N
    lines.append(f"  Status: {kstatus}  count={keys.get('key_count', 0)}  modified={keys.get('file_mtime', '?')}")
    if keys.get("changed"):
        lines.append(f"  {R}! {keys.get('change_detail', 'File changed since last check')}{N}")
    for k in keys.get("keys", []):
        lines.append(f"    {C}{k['id']}{N}: {k['prefix']}  ({k['label']})")

    # Request Stats
    lines.append(f"\n{B}  Request Stats{N}")
    stats = report["stats"]
    if stats.get("status") == "UNAVAILABLE":
        lines.append(f"  {R}Stats unavailable{N}")
    else:
        lines.append(f"  Total: {stats.get('total_requests', 0)}  success={stats.get('success', 0)}  errors={stats.get('errors', 0)}  timeouts={stats.get('timeouts', 0)}")
        lines.append(f"  Error rate: {_color_pct(stats.get('error_rate', 0), 5, 10)}%  Timeout rate: {_color_pct(stats.get('timeout_rate', 0), 3, 5)}%")
        if stats.get("avg_response_time"):
            lines.append(f"  Response: avg={stats['avg_response_time']}s  p90={stats.get('p90_response_time', '?')}s  max={stats.get('max_response_time', '?')}s")
        if stats.get("slow_requests"):
            lines.append(f"  {Y}Slow (>60s): {stats['slow_requests']}{N}")
        lines.append(f"  Tokens: {_fmt_tokens(stats.get('tokens_in', 0))} in / {_fmt_tokens(stats.get('tokens_out', 0))} out")

        by_model = stats.get("by_model", {})
        if by_model:
            for model, vals in by_model.items():
                lines.append(f"    {C}{model}{N}: {vals.get('count', 0)} reqs, {_fmt_tokens(vals.get('input', 0))} in / {_fmt_tokens(vals.get('output', 0))} out")

    # Recent Errors
    errors = report.get("errors", {})
    err_count = errors.get("count", 0)
    if err_count > 0:
        lines.append(f"\n{R}{B}  Recent Errors ({err_count}){N}")
        for e in errors.get("recent", [])[-5:]:
            lines.append(f"  {D}{e.get('ts', '?')[:19]}{N}  {e.get('endpoint', '?')}  {R}{e.get('error_type', '?')}{N}  {e.get('detail', '')[:80]}  ({e.get('elapsed', 0):.0f}s)")

    # Rate Limits
    limits = report["limits"]
    if isinstance(limits, dict) and not limits.get("status"):
        lines.append(f"\n{B}  Rate Limits{N}")
        for bucket, info in limits.items():
            rpm_pct = info.get("rpm_pct", 0)
            lines.append(f"  {info.get('label', bucket)}: rpm={info.get('rpm_used', 0)}/{info.get('rpm_limit', 0)} ({_color_pct(rpm_pct, 60, 80)}%)  daily={info.get('daily_requests', 0)} reqs")

    # Node Activity
    geo = report["geo"]
    if geo.get("status") != "UNAVAILABLE" and geo.get("active_nodes", 0) > 0:
        lines.append(f"\n{B}  Nodes{N}")
        for node, info in geo.get("nodes", {}).items():
            lines.append(f"    {C}{node}{N}: {info.get('count', 0)} reqs, last {info.get('last_seen', '?')[:19]}")

    lines.append(f"\n{D}{'─'*60}{N}\n")
    return "\n".join(lines)


def format_brief(report):
    """One-line summary."""
    health = report["health"]
    stats = report.get("stats", {})
    ts = report.get("tailscale", {})
    keys = report.get("keys", {})
    active = report.get("active", {})
    errors = report.get("errors", {})
    hchar = "OK" if health == "HEALTHY" else "!!"
    flags = ""
    if keys.get("changed"):
        flags += " KEYS-CHANGED"
    if active.get("stale_count", 0):
        flags += f" STALE:{active['stale_count']}"
    return (
        f"[{hchar}] proxy={report['proxy'].get('status', '?')} "
        f"upstream={report['upstream'].get('status', '?')} "
        f"ts={ts.get('status', '?')}({ts.get('peers', 0)}p) "
        f"reqs={stats.get('total_requests', 0)} "
        f"err={stats.get('error_rate', 0)}% "
        f"active={active.get('count', 0)} "
        f"errs={errors.get('count', 0)}{flags}"
    )


def _fmt_duration(seconds):
    if not seconds:
        return "0s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def _fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _color_pct(val, warn_threshold, error_threshold):
    if val >= error_threshold:
        return f"{R}{val}{N}"
    if val >= warn_threshold:
        return f"{Y}{val}{N}"
    return f"{G}{val}{N}"


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    watch = "--watch" in args
    brief = "--brief" in args

    if watch:
        interval = 30
        print(f"{B}Monitoring every {interval}s. Ctrl+C to stop.{N}\n")
        try:
            while True:
                report = generate_report()
                if brief:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"{D}{ts}{N} {format_brief(report)}")
                else:
                    print(format_report(report))
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n{D}Stopped.{N}")
    elif as_json:
        report = generate_report(as_json=True)
        print(json.dumps(report, indent=2))
    elif brief:
        report = generate_report()
        print(format_brief(report))
    else:
        report = generate_report()
        print(format_report(report))


if __name__ == "__main__":
    main()
