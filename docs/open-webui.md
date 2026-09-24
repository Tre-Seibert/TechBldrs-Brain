# Open WebUI lab notes

Lab UI is [Open WebUI](https://github.com/open-webui/open-webui) in front of tb-brain. tb-brain calls local Ollama. Do not build a custom chat app. Production UI later is inside Flow.

`docker-compose.yml` in this repo starts **Open WebUI only**. Chat is sent to tb-brain at `http://host.docker.internal:8765/v1` (model id `tb-brain`). Ollama stays on the host for tb-brain; the UI must not talk to `:11434`. Named volume `tb-brain-open-webui` lives in Docker's data root, not in this OneDrive repo.

## Start

1. Install [Ollama](https://ollama.com/) on Windows. Set `OLLAMA_MODELS` to a **non-OneDrive** disk (G: preferred; C: is tight). Example: `G:\ollama\models`.
2. Pull a local instruct model. Pick one that fits the current GPU; do not bake the size into code.

   ```powershell
   ollama pull qwen2.5:14b-instruct-q5_K_M
   # or
   ollama pull mistral-small:24b-instruct-2501-q4_K_M
   ```

3. Confirm Ollama: `curl.exe --noproxy "*" http://127.0.0.1:11434/api/tags`
4. Start tb-brain: `.\scripts\run.ps1` (must stay up; `BRAIN_HOST=0.0.0.0` so the container can reach it).
5. From the repo: `docker compose up -d`
6. Open `http://localhost:3000`. New chat, model **tb-brain**. Admin → Settings → Connections: Ollama off, only `http://host.docker.internal:8765/v1` with key `sk-tb-brain-lab`. Admin → Settings → Models → tb-brain: turn **Builtin Tools** off (Task Management's `update_task`, Notes, Calendar, Automations). The tool loop ignores those even if Open WebUI still sends them.

## How chat reaches tools

Open WebUI calls tb-brain `POST /v1/chat/completions`. `GET /v1/models` always returns id `tb-brain`. The tool loop maps that id to `LLM_MODEL` (local Ollama). Do not pick `llama3.1:8b` or `qwen2.5:7b-instruct` from an Ollama connection — those skip the tool loop.

```text
OPENAI_API_BASE_URL=http://host.docker.internal:8765/v1
OPENAI_API_KEY=sk-tb-brain-lab
```

Those are set in `docker-compose.yml`. `ENABLE_OLLAMA_API=false` so Compose cannot re-inject Ollama `:11434`.

## Sign in with Microsoft (lab SSO)

Techs sign into Open WebUI with the same Microsoft Entra tenant Flow uses. No chat-bubble auth, no IT Glue passwords, no OneDrive for secrets. Open WebUI does **not** create Flow users — a tech must already have an active account from Flow's own Entra login before they can act as anyone in Flow.

### 1. Register a separate Entra app

Do not reuse Flow's production app registration or its redirect URI. In the Azure portal:

1. **Entra ID → App registrations → New registration**
   - Name: `tb-brain lab (Open WebUI)`
   - Supported account types: same tenant as Flow (single tenant)
   - Redirect URI (platform **Web**): `http://localhost:3000/oauth/microsoft/callback` — use `localhost`, not `127.0.0.1`; Entra matches the redirect URI as an exact string, and `WEBUI_URL` (below) has to use the same host you register here
2. **API permissions**: Microsoft Graph → Delegated → `openid`, `email`, `profile` (usually pre-consented; grant admin consent if your tenant requires it).
3. **Certificates & secrets → New client secret**. Copy the secret *value* immediately — it's the only time it's shown.
4. **Overview** page: copy
   - Application (client) ID → `MICROSOFT_CLIENT_ID`
   - Directory (tenant) ID → `MICROSOFT_CLIENT_TENANT_ID`
   - Client secret value → `MICROSOFT_CLIENT_SECRET`

Put all three in this repo's host `.env` (never committed — `.env` is gitignored). If the tenant doesn't return an `email` claim, set `OAUTH_EMAIL_CLAIM=preferred_username` in compose and `OAUTH_EMAIL_CLAIM=preferred_username` in tb-brain's own `.env`.

Also set `WEBUI_URL` in that same `.env` to the **base** URL only — `http://localhost:3000`, not `http://localhost:3000/oauth/microsoft/callback`. Open WebUI appends the callback path itself; if `WEBUI_URL` already has it, sign-in fails with `AADSTS50011` (redirect URI mismatch) because Open WebUI builds an incorrect URI to send Entra.

### 2. Sign the identity hop

Open WebUI forwards the signed-in user to tb-brain as a **signed JWT**, not a raw header, so it can't be spoofed by anything else on the LAN reaching `:8765` (`BRAIN_HOST=0.0.0.0`):

```
openssl rand -hex 32
```

Put the same value in **two** places:
- Host `.env` → `FORWARD_USER_INFO_HEADER_JWT_SECRET` (compose reads this into the `open-webui` container)
- tb-brain's own `.env` → `BRAIN_USER_JWT_SECRET` (tb-brain verifies the JWT with this)

Without this secret, Open WebUI falls back to sending an unsigned `X-OpenWebUI-User-Email` header, which tb-brain treats as an unverified lab-only label — never forwarded to Flow as a trusted identity.

### 3. Recreate the container

```powershell
docker compose up -d --force-recreate
```

Open `http://localhost:3000` — the login screen should now offer "Sign in with Microsoft" alongside the existing password form.

### 4. Disable password sign-ups (without locking out the admin)

Click path, done while signed in as the existing admin — **do not** set `ENABLE_LOGIN_FORM=false` or `ENABLE_PASSWORD_AUTH=false` in compose; that hides the password form entirely and can lock out the admin if their account isn't OAuth-mapped yet:

1. Gear icon (top right) → **Admin Panel**
2. **Settings → General**
3. Toggle off **"Enable New Sign Ups"**

This blocks *new* password registrations only. The current admin's password login keeps working as a break-glass path. Verify it still works in a private/incognito window before closing your authenticated session.

`OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` is on in compose, so a Microsoft sign-in binds to the existing password-admin account by matching email instead of erroring with "this email is already registered." Upstream docs flag an account-takeover caveat if the OAuth provider doesn't reliably verify email — that doesn't apply here since this is a single-tenant Entra directory we control, not a public OAuth provider. Don't turn this on for a multi-tenant / public sign-in setup.

## What not to enable

- Cloud model connections (OpenAI, Anthropic, Ollama Cloud) for client data
- Web search / browser tools
- Write-back to Flow
- Storing chat exports with PHI in this OneDrive repo
- `ENABLE_LOGIN_FORM=false` / `ENABLE_PASSWORD_AUTH=false` until Microsoft sign-in is confirmed working for every admin account
- Sending `X-Brain-Actor-Email` to Flow for anything tb-brain hasn't verified via the signed JWT — `PRIVATE_API_TOKEN` plus that header can act as the matching Flow user, so an unverified email must never reach it
