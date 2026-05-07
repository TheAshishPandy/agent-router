# data/

Runtime state. Everything in this directory except `*.example.json` and this
README is gitignored. `make setup` creates the real files.

## Files

| File | Purpose | Mode |
|---|---|---|
| `dashboard-users.json` | Dashboard accounts (bcrypt-hashed passwords) | `0600` |
| `proxy-keys.json` | API keys for `/api/*` (`pcx-*` format) | `0600` |
| `tailscale.json` | CORS allowlist + node map metadata for the dashboard | `0644` |
| `config.json` | Optional file-based provider keys (env vars take precedence) | `0600` |
| `proxy-geo.json` | Auto-generated request distribution log for the dashboard map | `0644` |

## Manage by hand

**Add a dashboard user:**
```bash
python3 -c 'import bcrypt; print(bcrypt.hashpw(b"YOUR_PASSWORD", bcrypt.gensalt(12)).decode())'
# paste the output into data/dashboard-users.json under users.<name>.password_hash
```

**Add an API key:** preferred is `POST /api/keys` (Tailscale-trusted) from the
dashboard or curl. Manual format:
```json
{
  "keys": {
    "my-laptop": {
      "key": "pcx-my-laptop-abc123...",
      "label": "My MacBook",
      "created": "2026-05-07T12:00:00Z"
    }
  }
}
```
Keys are matched by exact string. Format is `pcx-<id>-<48 hex chars>`.

**Configure CORS / dashboard map:** edit `tailscale.json`:
```json
{
  "endpoint": "http://your-tailnet-host:8001",
  "allowed_origins": ["https://your-frontend.example.com"],
  "nodes": {
    "your-tailnet-host": { "lat": -33.9, "lng": 151.2, "name": "Your Server" }
  }
}
```

## Backup

The whole directory (except `proxy-geo.json`, which is regenerable) is worth
backing up. Three small JSON files = your auth state. `tar czf data-backup.tgz
data/dashboard-users.json data/proxy-keys.json data/tailscale.json` and store
it somewhere safe.
