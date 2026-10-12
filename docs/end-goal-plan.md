# tb-brain end-goal plan

Written 2026-10-12. Where the code needs to be before we buy the Mac Studio M5 Ultra.

## The goal

A tech asks any question about a client we manage. tb-brain works out which system holds the
answer (Flow, Datto RMM, the client's Microsoft 365 tenant via GDAP, IT Glue, and later more), calls
read-only tools on those systems, and answers from what they returned. A local model does all of it
(client data includes PHI).

Hardware decision and pricing: https://claude.ai/artifact/XDDgExisc6ph9x6dKbAyTY

## Decisions made

| Decision | Choice |
|---|---|
| Hardware | Mac Studio M5 Ultra, 96 GB, 80-core GPU. Bought only after this plan's "ready to buy" list is done, then tested inside Apple's 14-day return window. The 5060 Ti box stays as the helper and fallback. |
| Models to test on the Mac | Qwen 3.6 27B (primary), a 30B-A3B MoE (speed option), Qwen3 32B (fallback). Thinking off. |
| How tb-brain reaches Datto and Microsoft 365 | **Directly**, with its own read-only credentials. Flow has no copy of those APIs. Flow stays the system of record for tickets/time/mail/contacts and supplies the client identity map. |
| Microsoft 365 access model | One multi-tenant app registration, **application (app-only) permissions, read-only**, consented once per customer tenant. The permission list is final before anyone consents (see below), so each tenant is consented once. |
| Accuracy work on the 5060 Ti | Stop adding 14B-only fixes (new question-shape routers, prompt wording to coax qwen2.5:14b). Keep tool-side fixes (date math, filters, ranking) because they help every model. |
| Rules that carry over unchanged | Local model only. Read-only by default (merge_tickets stays the one gated write). Every tool call audited. No passwords or secrets. Runtime data and secrets off OneDrive and out of git. |

## Where the code is today (2026-10-12)

Strong:

- Safety model: read-only by default, merge gate, signed identity, audit log.
- 339 unit tests pass. 60 Flow eval cases scored on tool calls, 28 router cases, 59-question live run.
- Counting and date math happen in code, not in the model.
- Model and endpoint come from env (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_EXTRA_BODY`, `LLM_NO_THINK`).
- The client identity map mostly exists already: `ClientDetail` carries `m365_tenant_id` (Flow fills it
  from the partner contracts list) and `itglue_url` (holds the IT Glue org id). Datto site names start
  with the client code.

Gaps:

| # | Gap | Evidence |
|---|---|---|
| 1 | Single-source architecture | `FlowSource` is threaded through `dispatch()`, `run_tool()`, `run_tool_loop()`. No connector concept. |
| 2 | Prompt already ~8K tokens | System prompt ~3.3K tokens + 14 tool schemas ~4.7K (chars/4 estimate) against an 8K context on the runtime box. |
| 3 | Prompt is Flow-only and written for 14B | ~40 "question → tool call" recipes; refuses anything "not about Flow data". |
| 4 | 14B workarounds in code | 72 places in `handlers.py` re-read the user's sentence with regexes; 5 routers; Flow-only word lists in `loop.py` (`_DATA_WORD_RE`, nudges, fallbacks). |
| 5 | `handlers.py` is 3,578 lines | Every Flow tool, formatter and heuristic in one file. |
| 6 | Streaming isn't real | `stream_final_message` runs the whole loop, then sends the text. No progress lines. |
| 7 | Prompt layout defeats caching | Signed-in user and date sit inside the system message, before the tool list in Qwen's template, so each user/day probably re-reads the tool tokens. |
| 8 | Eval can't measure the Mac decision | Flow-only, stub-only, no per-step timing, no concurrency test. |
| 9 | Knowledge sync is manual | `scripts/sync_knowledge.py` run by hand. |

## Phase A: Foundations

Do these before adding any source.

| Step | Work | Size |
|---|---|---|
| A1 | Split `app/tools/handlers.py` by domain (tickets, time, mail, contacts, clients, knowledge). No behavior change; all tests green. | M |
| A2 | Connector layout: `app/sources/<name>/` per source with its read-only protocol, stub fixtures, HTTP adapter, tools, a short prompt section and eval cases. Each enabled by env (`DATTO_MODE=stub|http`, `M365_MODE=stub|http`) like `FLOW_MODE`. Flow moves into the same layout. | L |
| A3 | Client resolver: client code or name → `{client_code, name, m365_tenant_id, itglue_org_id, datto_site_uid}`. Tenant and IT Glue ids come from Flow's client detail; the Datto site comes from matching Datto site names to the code. Cached. Source tools call it; the model never handles tenant ids. Clear message when a client has no tenant id or no Datto site. | M |
| A4 | Slim the system prompt to general rules (safety, read-only, English, answer from tools only) plus a short section per enabled source. Tool-specific guidance moves into tool descriptions. Remove the "Flow only" refusal. Target ≤ 1.5K tokens base. | M |
| A5 | Source routing: pick which sources' tools to offer per call. Keywords first, the model choosing a source as fallback, Flow core always on. Target ≤ ~15 tools per call. Tool names carry the source prefix (`datto_`, `m365_`). | M |
| A6 | Cache-friendly prompt order: static rules and tools first; signed-in user and date in a later message. Check cache hits with `prompt_tokens`. | S |
| A7 | Make the loop's Flow-only workarounds source-aware (`_DATA_WORD_RE`, the must-use-a-tool nudge, fallback messages). Leave routers alone until the Mac eval. | S |
| A8 | Update README: goal, sources, hard rules for new sources, out-of-scope list. | S |

## Phase B: Datto RMM

Questions: [eval/datto_questions_draft.md](../eval/datto_questions_draft.md).

| Step | Work | Size |
|---|---|---|
| B0 | A read-only Datto RMM API user for tb-brain (its own key, not Flow's). Key and secret in the runtime box `.env`. | S (admin) |
| B1 | Tools: `datto_list_devices`, `datto_get_device`, `datto_alerts`, `datto_site_summary`, `datto_patch_report`, `datto_device_software`. Each returns a summary capped in size, never raw API output. | L |
| B2 | Stub fixtures shaped like Datto API v2 responses for 3 fake sites. | M |
| B3 | Decide `list_machines` (Flow's machine records) vs `datto_list_devices` (live). Likely: Datto answers live status; Flow answers "what machines are on our records/tickets". | S |
| B4 | Eval cases from the questions draft, including cross-source with Flow. | M |

## Phase C: Microsoft 365 via GDAP

Questions: [eval/m365_questions_draft.md](../eval/m365_questions_draft.md).

| Step | Work | Size |
|---|---|---|
| C0 | Multi-tenant app registration with the permission list below; consent per tenant **after** the list is final. Secret or certificate in the runtime box `.env`. | M (admin) |
| C1 | Tools: `m365_users`, `m365_get_user`, `m365_mfa_report`, `m365_license_summary`, `m365_signins`, `m365_security`, `m365_policies`, `m365_devices`, `m365_mailboxes`, `m365_tenant`, `m365_audit`. All summaries, capped. | L |
| C2 | Stub fixtures for 3 fake tenants (one with Business Premium, one Business Standard, one with report name concealment on). | M |
| C3 | Eval cases from the questions draft, including cross-source with Flow and Datto. | M |
| C4 | Later, optional: Graph search for mail that was never filed on a ticket (TechBldrs' own mailbox, not clients'). | M |

### Microsoft Graph application permissions (all read-only)

Consent each tenant once, after this list is final.

| Permission | Used for |
|---|---|
| `User.Read.All` | Users, account enabled/disabled, guests |
| `Directory.Read.All` | Directory objects, groups, org info |
| `GroupMember.Read.All` | Group membership |
| `RoleManagement.Read.Directory` | Who holds admin roles |
| `Organization.Read.All` | Licenses (subscribed SKUs), tenant info |
| `LicenseAssignment.Read.All` | Per-user license assignment |
| `Domain.Read.All` | Domains and verification |
| `AuditLog.Read.All` | Sign-in logs, last sign-in (`signInActivity`), directory audit log, MFA registration report |
| `UserAuthenticationMethod.Read.All` | Per-user MFA methods (works without Entra P1) |
| `Reports.Read.All` | Mailbox usage, active users, MFA registration details |
| `Policy.Read.All` | Conditional Access, security defaults, auth method policies |
| `IdentityRiskyUser.Read.All` | Risky users (needs Entra P2 for full data) |
| `IdentityRiskEvent.Read.All` | Risk detections (P2) |
| `SecurityEvents.Read.All` | Secure Score |
| `SecurityAlert.Read.All` | Defender alerts |
| `SecurityIncident.Read.All` | Defender incidents |
| `DeviceManagementManagedDevices.Read.All` | Intune devices, compliance state |
| `DeviceManagementConfiguration.Read.All` | Intune compliance and config policies |
| `MailboxSettings.Read` | Inbox rules (forwarding rules), shared vs user mailbox |
| `Application.Read.All` | App registrations and enterprise apps, expiring secrets |
| `ServiceHealth.Read.All` | Microsoft 365 service health and incidents |

Deliberately **not** requested (PHI and least privilege): `Mail.Read`, `Mail.ReadBasic`,
`Files.Read.All`, `Sites.Read.All`, `Chat.Read.All`, `Calendars.Read`, and anything `*.ReadWrite*`.
tb-brain never reads mail bodies, files or chats in client tenants.

Notes to verify before consenting:

- Granting Microsoft Graph application permissions needs Privileged Role Administrator or Global
  Administrator in the customer tenant, so the GDAP relationship needs that role (time-boxed) for the
  consent step. Techs' day-to-day GDAP roles can stay Global Reader / Security Reader.
- Sign-in logs, `signInActivity` and Conditional Access need Entra ID P1 (included in Business
  Premium). Risky users need P2. Tools must say "this tenant's license doesn't include that" instead
  of failing.
- Tenants with "conceal user names in reports" on return hashed names from report endpoints. Tools
  must detect and say so.
- Exchange-level forwarding (`ForwardingSmtpAddress`) is not in Graph. It needs the Exchange Online
  admin API with `Exchange.ManageAsApp` plus an Exchange role. Left out of the first pass; inbox
  rules cover most forwarding cases.

## Phase D: Runtime and user experience

| Step | Work | Size |
|---|---|---|
| D1 | Real streaming of the final answer, plus status lines while tools run ("Checking Microsoft 365 for WDON…"). | M |
| D2 | Per-step timing in `x_tb_brain`: prompt reading, generation, each tool's time. | S |
| D3 | Concurrency: N requests at once without blocking (async throughout, shared HTTP clients, per-source rate limits). | S |
| D4 | Tool result caps: every tool limits and summarizes output. Protects the Mac's slow prompt reading. | S |
| D5 | Audit rows for Datto and Microsoft 365 calls carry client code and tenant id. | S |

## Phase E: Benchmark kit for the 14 days

| Step | Work | Size |
|---|---|---|
| E1 | Multi-source eval set from B4 + C3, 40+ cases. | (B, C) |
| E2 | Run-matrix script: models × quantization (Q4, Q6) × routers on/off. Reports accuracy, p50/p95 latency, per-step times. | M |
| E3 | Concurrency test: 1, 3, 5 questions at once; report slowdown. | S |
| E4 | Baseline run on the 5060 Ti with the full new eval. | S |
| E5 | Acceptance criteria, written before buying: simple question starts ≤ 3 s; multi-source done ≤ 15 s; accuracy beats the 14B baseline; 3 at once under 2× slowdown. | S |

## Left until after the Mac

- Retire routers and 14B workarounds, decided from the routers on/off matrix on the Mac.
- More IT Glue (configurations, contacts, domains; never passwords), live knowledge sync.
- More sources (backup, Pax8, firewall, email security).
- Production UI inside Flow.

## Ready to buy when

- [ ] Phase A done, tests green
- [ ] Datto working against the live API, read-only
- [ ] Microsoft 365 working on 2–3 real tenants (or stubs if consent is still pending)
- [ ] 40+ multi-source eval cases with a 5060 Ti baseline run
- [ ] Run-matrix, timing and concurrency scripts working
- [ ] Acceptance criteria written down
