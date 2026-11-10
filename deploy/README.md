# Deploying the MCP server on the dev box (10.10.0.91)

The box already runs nginx with Let's Encrypt for several `*.techbldrs.dev` names, and the office IP forwards 80/443
to it. tb-brain is added the same way: its own systemd service on loopback, one more nginx vhost, one more
certificate. Nothing here touches `ticketing.service` or any other service on the box.

Everything below runs as `tbadmin`. Steps marked `sudo` need the account's password.

## 1. Code and venv

```bash
cd /home/tbadmin
git clone https://github.com/Tre-Seibert/TechBldrs-Brain.git     # or: git -C TechBldrs-Brain pull
cd TechBldrs-Brain
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m unittest discover -s tests                     # 680+ tests, hermetic, ~2 s
mkdir -p /home/tbadmin/tb-brain-data
```

## 2. Settings (`/home/tbadmin/TechBldrs-Brain/.env`, chmod 600)

Start from `.env.example`. The values that matter here:

| Setting | Value |
|---|---|
| `FLOW_MODE` / `FLOW_BASE_URL` / `FLOW_API_TOKEN` | `http`, `https://flow.techbldrs.com`, Flow's private API token |
| `DATTO_MODE` / `DATTO_API_URL` / `DATTO_API_KEY` / `DATTO_API_SECRET` | `http`, the Datto RMM API URL, tb-brain's own read-only API user |
| `M365_MODE` | `off` until the Graph app is consented (Phase 2) |
| `MCP_AUTH` | `entra` |
| `MCP_ENTRA_TENANT_ID` | the TechBldrs tenant id |
| `MCP_ENTRA_AUDIENCE` | the MCP app registration's Application ID URI, `api://<client id>`, plus `,<client id>` |
| `MCP_ENTRA_SCOPE` | `access_as_user` |
| `BRAIN_ACTOR` | `mcp-dev-box` (the audit label when no technician identity is available) |
| `LLM_*`, `QDRANT_URL`, `PROMPT_STYLE`, routers | unused by the MCP endpoint; leave at defaults |

`BRAIN_HOST`, `BRAIN_PORT` and `BRAIN_DATA_DIR` are pinned in the unit file.

## 3. Service

```bash
sudo cp deploy/tb-brain.service /etc/systemd/system/tb-brain.service
sudo systemctl daemon-reload
sudo systemctl enable --now tb-brain
systemctl status tb-brain --no-pager
curl -s http://127.0.0.1:8765/health | python3 -m json.tool | head -40
```

## 4. nginx and TLS

DNS first: an A record `brain.techbldrs.dev -> 71.185.235.43` in Cloudflare, DNS-only (grey cloud) like the other
names. Check with `dig +short brain.techbldrs.dev @8.8.8.8`.

```bash
sudo cp deploy/nginx-brain.techbldrs.dev.conf /etc/nginx/sites-available/brain.techbldrs.dev
sudo ln -s /etc/nginx/sites-available/brain.techbldrs.dev /etc/nginx/sites-enabled/brain.techbldrs.dev
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d brain.techbldrs.dev
```

certbot rewrites the vhost with the 443 listener and the 80 -> 443 redirect, as it did for the other sites.

## 5. Smoke test

From any machine, with an Entra access token for the app (or, with `MCP_AUTH=api_key` on a lab box, the key):

```bash
python scripts/mcp_smoke.py --url https://brain.techbldrs.dev/mcp --token "$TOKEN" list
python scripts/mcp_smoke.py --url https://brain.techbldrs.dev/mcp --token "$TOKEN" call list_tickets client_code=WDON stage=open limit=3
```

Without a token the endpoint answers `401` with `WWW-Authenticate: Bearer`. Every call lands in
`/home/tbadmin/tb-brain-data/logs/tool_calls.jsonl` with the technician's UPN as `actor`.

## Updating

```bash
cd /home/tbadmin/TechBldrs-Brain && git pull && venv/bin/pip install -r requirements.txt && sudo systemctl restart tb-brain
```

## Removing

```bash
sudo systemctl disable --now tb-brain && sudo rm /etc/systemd/system/tb-brain.service && sudo systemctl daemon-reload
sudo rm /etc/nginx/sites-enabled/brain.techbldrs.dev && sudo nginx -t && sudo systemctl reload nginx
```

The certificate can stay (`certbot delete --cert-name brain.techbldrs.dev` removes it).
