# OpenClaw — later, not the default install

Do **not** install OpenClaw as part of tb-brain lab setup. The default lab UI is Open WebUI in front of local Ollama. Production UI later lives inside Flow (Entra, tickets, techs).

OpenClaw is a valid later locked-down agent/gateway (Control UI + optional Teams). Extra OpenClaw features are opt-in. If it is ever used, it must be a subset of tb-brain, not a second brain.

## When it might be worth it

- A locked-down Control UI for a small set of techs
- Optional Microsoft Teams channel that still hits the same Flow tools
- Not a replacement for Open WebUI in the lab, and not a custom pretty chat app

## Lock-down checklist (required if installed)

Copy this list before any install. If an item cannot be met, do not run OpenClaw.

1. **Local Ollama only.** Native API `http://127.0.0.1:11434` with **no `/v1`**. Do not point OpenClaw at tb-brain's OpenAI-compat `/v1` and do not point it at Ollama Cloud.
2. **No cloud LLM.** Client data includes dental/PHI. Same rule as tb-brain.
3. **No exec, browser, or files tools.** Disable shell, filesystem, and browser plugins. Flow skills only.
4. **No WhatsApp, iMessage, or Telegram.** Teams is the only optional chat channel, and only if explicitly enabled later.
5. **Lean tool catalog.** Same stable names as tb-brain: `search_contact`, `latest_ticket`, `list_mail`. Do not add write tools (send-mail, close-ticket, IT Glue password injection).
6. **Read-only.** Same as tb-brain. No mutations against Flow, Graph, Datto, or IT Glue.
7. **Do not auto-inject IT Glue passwords** into prompts.
8. **Audit every tool call** (actor, client_code, tool, row ids) to a log directory **off OneDrive**.
9. **LLM base URL and model from env**, not hostname or "14B" baked into config files that get copied to the next GPU box.
10. **Bind localhost.** Control UI and any gateway listen on `127.0.0.1` unless there is a written reason to expose them on the LAN.

## What OpenClaw must call

Prefer tb-brain's HTTP tools (or the future Flow `/api/private/brain/*` read-only API) over giving OpenClaw database credentials.

```text
POST http://127.0.0.1:8765/tools/search_contact
POST http://127.0.0.1:8765/tools/latest_ticket
POST http://127.0.0.1:8765/tools/list_mail
```

## Explicitly out of scope

- Installing OpenClaw in `docker-compose.yml` for this repo
- Ollama Cloud, OpenAI, Anthropic, or any hosted model for tenant/client data
- A second vector index of Flow tickets/mail
- Write actions of any kind
