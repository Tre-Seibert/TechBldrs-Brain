# tb-brain on Microsoft Copilot Studio: plan

Written 2026-11-10. Approved by Tre the same day (decisions below). Supersedes the hardware track in
[end-goal-plan.md](end-goal-plan.md); the end goal itself is unchanged.

## Why

We cannot afford hardware that runs a local model reliably, so Microsoft hosts the model instead. The "local LLM
only" rule is dropped. These rules stay: read-only by default, every tool call audited, no IT Glue passwords
anywhere the agent can reach, no secrets in git. The last local-LLM state is tagged `local-llm-final`.

## What exists and what changes

The handler layer is reused as it is. Nothing under `app/sources/` imports the LLM loop. The runtime contract is
`Sources.dispatch(name, args, turn, knowledge) -> ToolResult`, wrapped by `run_tool()` for validation and the audit
row. Two coupling points:

- `ChatTurn` (the user's sentence) feeds 14B workarounds in Flow handlers. It is optional everywhere; the MCP layer
  passes `None`.
- `ToolResult` has three shapes: `reply` (show as-is), `digest` + `footer` (model writes prose, exact figures
  appended), `chunks` (map-reduce that needs an LLM; four Flow handlers). The MCP layer flattens these to text.

Tools today: Flow 12 read tools plus the gated `merge_tickets`, Datto 6, Microsoft 365 11, and `search_knowledge`.

## Architecture (decided)

1. **One agent**, "tb-brain", on the Copilot Studio **standard harness** with generative orchestration and
   "Authenticate with Microsoft". The agent's instructions carry the old system prompt's rules.
2. **One MCP server in this repo** (official `mcp` SDK 2.x, Streamable HTTP, stateless, JSON responses) mounted in
   the existing FastAPI app at `/mcp` (every enabled source) and `/mcp/<source>` (one source), so a later split into
   child agents is a Copilot Studio change, not a code change. Tools come from each `SourceSpec`'s schemas and run
   through `run_tool()`, so validation, audit rows and the sources' own error wording are unchanged. Not exposed:
   `merge_tickets` (the only write) and `search_knowledge` (its embeddings need a model host; parked).
3. **Hosted on the dev box 10.10.0.91** as `tb-brain.service` under `tbadmin`, bound to 127.0.0.1:8765, behind a
   new nginx vhost `brain.techbldrs.dev` with a Let's Encrypt certificate and a Cloudflare A record (DNS-only, like
   the other `*.techbldrs.dev` names there). `ticketing.service` is not touched.
4. **Tool auth is Microsoft Entra OAuth 2.0** (Copilot Studio "Manual" mode) against one app registration that
   exposes the scope `access_as_user`. The server validates the token (tenant, audience, signature) and the tech's
   UPN becomes the audit actor and Flow's `X-Brain-Actor-Email`. An API-key mode exists only for the smoke test
   script and is off by default.
5. **Sources keep their own read-only credentials** in the box's `.env` (Flow brain API token, Datto API user,
   the multi-tenant Graph app). Flow stays the client identity map. The Graph permission list in
   [end-goal-plan.md](end-goal-plan.md) is final before any tenant is consented.
6. **Live data from the start** (`FLOW_MODE=http`, `DATTO_MODE=http`); stubs stay for tests.
7. **Transcripts off** in the Copilot Studio environment (PHI). Copilot Studio still keeps session data 28 days.

### Rejected

| Option | Why not now |
|---|---|
| GitHub Copilot harness | MCP there is preview, billed per token for every user, no conversion path. Candidate for an A/B in Phase 2 (the MCP server is harness-neutral). |
| Parent plus one connected agent per source | Extra hop and description drift for 18-29 tools; Microsoft's own threshold is 30-40 actions. The per-source endpoints keep the option open. |
| REST API tool from OpenAPI | One static connector action per tool, no dynamic refresh. |
| HTTP request node | Lives in classic topics; the orchestrator does not pick it like a tool. |
| Entra App Proxy, on-premises data gateway, Azure Relay, Cloudflare Tunnel | The box is already public. App Proxy and the gateway are undocumented for MCP (one public report of the session header being dropped by the gateway). Cloudflare Tunnel is the fallback if the port-forward ever goes away. |

## Dev box (inspected 2026-11-10)

Ubuntu 24.04.4 on a KVM VM, 4 vCPU, 15 GB RAM, about 15 GB disk free. Python 3.12.3 with venv. nginx 1.24 on
80/443 with certbot certificates for 16 `*.techbldrs.dev` names; the office IP 71.185.235.43 forwards 80/443 to it,
and `techbldrs.dev` DNS is on Cloudflare (DNS-only). Outbound HTTPS to Microsoft login, Graph, PyPI, GitHub and
flow.techbldrs.com works. ufw active. No tunnels. About 20 app services; port 8765 free. `tbadmin` has no
passwordless sudo and cannot use Docker. GitHub credentials for a clone are present.

## Repo changes

- `app/mcp/`: the server (tools from `SourceSpec`s, result-to-text, dispatch through `run_tool`), Entra and API-key
  bearer verification, identity binding to the existing context variables. Tests run every tool through the MCP
  path with `turn=None`.
- Result-to-text rule: `reply` as-is; `digest` then `footer` marked as exact figures; `chunks` collapsed to the
  digest and fallback (Copilot's model summarizes in one pass); errors as the source's own message.
- Settings: `MCP_AUTH` (`entra` | `api_key` | `off`), `MCP_ENTRA_TENANT_ID`, `MCP_ENTRA_AUDIENCE`,
  `MCP_API_KEY`. `deploy/` holds the systemd unit and nginx vhost templates and the box runbook.
- `agent/`: the Copilot Studio VS Code extension's clone of the agent (YAML), git-tracked.
- `scripts/mcp_smoke.py` calls every tool through the MCP endpoint without Copilot Studio.
  `scripts/eval_mcp.py` exports the 172 eval cases as a Copilot Studio test set and scores tool calls from
  `tool_calls.jsonl` by time window, so a manual run in the test pane still gets today's tool-call scoring.
- Marked legacy, kept until Phase 4: `app/agent/` (loop, routing, system prompts, wording, passthrough), the five
  routers in Flow's handlers, `lean_tools.py`, the per-source `prompt.py`, `/v1/chat/completions`, Open WebUI
  docs and `docker-compose.yml`, `scripts/hw_eval.ps1`, `probe_*.py`, `live_run.py`, `eval.py`, `app/knowledge/`.
  The secret-request refusal becomes an agent instruction plus a test.

## Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 Prep | `config.py` restored, tag pushed, plan committed, Cloudflare A record, Entra app registration | Tests green, tag on origin |
| 1 Pilot | MCP server with Flow + Datto, deployed behind TLS, attached to the agent with Entra sign-in, 15 eval questions by hand in the test pane | 13/15 correct tool picks, audit rows name the tech, answers under 10 s |
| 2 Full set | Microsoft 365 tools after tenant consent, all 172 cases through Copilot Studio evaluations, A/B the GitHub Copilot harness if accuracy disappoints, single vs child agents decided from data | Scores at or above the 14B baseline |
| 3 Production | Licensing, publish to Teams / Microsoft 365 Copilot, transcript and DLP policy, production host decision, IT Glue source | Techs using it |
| 4 Cleanup | Delete legacy code, rewrite README | |

## Risks

- The trial cannot publish, so automated evaluation and the client SDK wait for a license.
- Tool selection with 25-30 tools is the open accuracy question; Phase 1 answers it early.
- Connector timeouts are undocumented; the "all clients" Microsoft 365 tools may need splitting.
- The endpoint is public: every unauthenticated request is rejected, nginx rate-limits, the app binds to localhost.
- A tenant DLP policy could block custom connectors (MCP counts as one).
- PHI reaches Microsoft's cloud. Copilot Studio's own page says it is covered by the HIPAA BAA; confirm against the
  BAA's service list.
- The repo is edited outside Claude sessions (an uncommitted `config.py` edit from 2026-10-27 broke the import; it
  was backed up and restored on 2026-11-10).

## Rollback

`git checkout local-llm-final` restores the whole local-LLM setup. Legacy code stays through Phase 3, so the old
loop runs from the same checkout. Removing the nginx vhost and `tb-brain.service` returns the box to today.

## Licensing notes

Rate card (standard harness): classic answer 1 credit, generative answer 2, agent action (a tool call) 5, tenant
graph grounding 10. Test chat is free. Users with a Microsoft 365 Copilot license are not charged for agents in
Copilot, Teams or SharePoint (tools included, fair use). Pay-as-you-go and packs are priced in Microsoft's licensing
guide; third parties consistently quote $0.01 per credit and $200 per 25,000-credit pack. Rough real-use cost
without Microsoft 365 Copilot licenses: 7-17 credits a question, so 10 techs at 20 questions a day is roughly
$300-750 a month pay-as-you-go. TechBldrs holds no Copilot licenses today; Tre has approval for one license for
himself (which one is decided in Phase 3).

## Decisions taken 2026-11-10

| # | Question | Decision |
|---|---|---|
| 1 | Broken `config.py` on disk | Restore the committed version (backup kept outside the repo) |
| 2 | Hostname | `brain.techbldrs.dev`; Tre adds the Cloudflare record |
| 3 | Harness | Standard |
| 4 | Agent shape | Single agent; child agents later only from eval data |
| 5 | Tool auth | Microsoft sign-in (Entra OAuth) from the pilot |
| 6 | Pilot data | Live Flow and Datto from the start |
| 7 | Transcripts | Off |
| 8 | Licensing | No licenses held; one approved for Tre; path decided in Phase 3 |
| 9 | Knowledge search | Parked (legacy) |
| 10 | This document | Committed to the repo |

## Sources

Microsoft Learn, read 2026-11-10 under https://learn.microsoft.com/en-us/microsoft-copilot-studio/ unless another
path is shown: MCP (`mcp-add-existing-server-to-agent`, `agent-extend-action-mcp`), Add tools
(`add-tools-custom-agent`), Quotas (`requirements-quotas`), Harnesses (`harnesses-overview`,
`guidance/choose-harness`), GitHub Copilot harness MCP and billing (`agents-experience/tools-add-mcp-server`,
`agents-experience/billing-credit-overview`), Other agents (`authoring-add-other-agents`, `add-agent-child-agent`,
`guidance/multi-agent-patterns`), Authentication (`configuration-end-user-authentication`,
`configure-enduser-authentication`, `advanced-connectors`), Licensing (`billing-licensing`,
`requirements-messages-management`, `faq-billing-licensing`, Power Platform `admin/pay-as-you-go-set-up`), VS Code
extension (`visual-studio-code-extension-overview`, `visual-studio-code-extension-synchronization`), Data
(`admin-transcript-controls`, `security-and-governance`, `data-location`, `admin-certification`), Evaluations
(`analytics-agent-evaluation-rest-api`, `agents-experience/analytics-agent-evaluation-intro`), Entra
(`entra/agent-id/secure-mcp-server-with-entra-id`).
