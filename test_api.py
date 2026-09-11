"""test_api.py - smoke test every endpoint on the running Agent Router."""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8001/api"

# --- Load API key from data/proxy-keys.json ---
try:
    with open("data/proxy-keys.json") as f:
        KEYS = json.load(f)["keys"]
    KEY_ID = next(iter(KEYS))
    KEY = KEYS[KEY_ID]["key"]
    print(f"Using key '{KEY_ID}': {KEY[:20]}...")
except Exception as e:
    print(f"FATAL: could not read data/proxy-keys.json: {e}")
    sys.exit(1)


def call(method, path, body=None, auth=True, timeout=60):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["x-api-key"] = KEY
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            cascade = r.headers.get("x-cascade", "")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        cascade = e.headers.get("x-cascade", "")
        status = e.code
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}", ""
    # Try to parse as JSON; fall back to raw
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw
    return status, cascade, parsed, raw


def show(name, status, cascade, parsed):
    mark = "OK " if status and 200 <= status < 300 else "ERR"
    hdr = f" [x-cascade={cascade}]" if cascade else ""
    body_preview = ""
    if isinstance(parsed, dict):
        if "content" in parsed and isinstance(parsed["content"], list):
            for blk in parsed["content"]:
                if blk.get("type") == "text":
                    body_preview = blk["text"][:60]
                    break
        elif "error" in parsed:
            body_preview = f"{parsed.get('error')}: {parsed.get('message','')[:60]}"
        elif "message" in parsed:
            body_preview = str(parsed["message"])[:60]
        else:
            body_preview = json.dumps(parsed)[:60]
    else:
        body_preview = str(parsed)[:60]
    print(f"{mark} {name:<22} {status}{hdr}  {body_preview}")


print()
print("=" * 78)
print("Agent Router - endpoint smoke test")
print("=" * 78)

# 1. Health (no auth)
status, cascade, parsed, raw = call("GET", "/health", auth=False)
show("GET /health", status, cascade, parsed)

# 2. /help (no auth)
status, cascade, parsed, raw = call("GET", "/help", auth=False)
show("GET /help", status, cascade, parsed)

# 3. /v1/messages - non-streaming (the main path)
body = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Say ok"}],
}
status, cascade, parsed, raw = call("POST", "/v1/messages", body)
show("POST /v1/messages", status, cascade, parsed)
if status == 200 and isinstance(parsed, dict):
    print(f"     model returned: {parsed.get('model')}")

# 4. /v1/messages - streaming
body_stream = dict(body, stream=True)
url = BASE + "/v1/messages"
req = urllib.request.Request(
    url,
    data=json.dumps(body_stream).encode(),
    headers={"Content-Type": "application/json", "x-api-key": KEY},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        cascade = r.headers.get("x-cascade", "")
        events = 0
        saw_start = False
        saw_stop = False
        for line in r:
            line = line.decode("utf-8", errors="replace").rstrip()
            if line.startswith("event:"):
                events += 1
                if "message_start" in line:
                    saw_start = True
                if "message_stop" in line:
                    saw_stop = True
        ok = saw_start and saw_stop
        mark = "OK " if ok else "ERR"
        print(
            f"{mark} {'POST /v1/messages(SSE)':<22} "
            f"{'200' if ok else '?'} [x-cascade={cascade}]  "
            f"events={events} start={saw_start} stop={saw_stop}"
        )
except Exception as e:
    print(f"ERR POST /v1/messages(SSE)  {type(e).__name__}: {e}")

# 5. /stats
status, cascade, parsed, raw = call("GET", "/stats")
show("GET /stats", status, cascade, parsed)

# 6. /limits
status, cascade, parsed, raw = call("GET", "/limits")
show("GET /limits", status, cascade, parsed)

# 7. /network-status
status, cascade, parsed, raw = call("GET", "/network-status")
show("GET /network-status", status, cascade, parsed)

# 8. /geo
status, cascade, parsed, raw = call("GET", "/geo")
show("GET /geo", status, cascade, parsed)

# 9. /swarm (trusted-network only on localhost, so should be allowed)
status, cascade, parsed, raw = call("GET", "/swarm")
show("GET /swarm", status, cascade, parsed)

# 10. /monitor (trusted-network only)
status, cascade, parsed, raw = call("GET", "/monitor", timeout=15)
show("GET /monitor", status, cascade, parsed)

# 11. /errors
status, cascade, parsed, raw = call("GET", "/errors")
show("GET /errors", status, cascade, parsed)

# 12. /active
status, cascade, parsed, raw = call("GET", "/active")
show("GET /active", status, cascade, parsed)

# 13. /platform-health
status, cascade, parsed, raw = call("GET", "/platform-health", timeout=15)
show("GET /platform-health", status, cascade, parsed)

# 14. Auth failure path - bad key
bad_req = urllib.request.Request(
    BASE + "/stats",
    headers={"x-api-key": "pcx-nonexistent"},
    method="GET",
)
try:
    urllib.request.urlopen(bad_req, timeout=10)
    print(f"ERR GET /stats(bad key)         200  (expected 401!)")
except urllib.error.HTTPError as e:
    mark = "OK " if e.code == 401 else "ERR"
    print(f"{mark} {'GET /stats(bad key)':<22} {e.code}  (expected 401)")

print()
print("=" * 78)
print("Legend: OK=2xx  ERR=4xx/5xx or exception")
print("The x-cascade header on /v1/messages tells you which provider served it.")
print("=" * 78)