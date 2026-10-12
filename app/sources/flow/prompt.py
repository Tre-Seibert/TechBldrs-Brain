"""Flow's section of the system prompt.

Sent only with PROMPT_STYLE=lean, after the base rules (app/agent/system.py). PROMPT_STYLE=legacy sends
SYSTEM_PROMPT, which carries the full Flow guidance itself. Per-tool guidance belongs in the lean tool
descriptions (app/sources/flow/lean_tools.py), not here.
"""

PROMPT_SECTION = """Flow (tickets, time entries, ticket-attached mail, client contacts, technicians, client accounts, machine records):
- Ticket labels are {CLIENT_CODE}-{ticket_num}, e.g. ZINT-5466. Client codes are short tokens the user names (ZTB, BUCK). Never use a client the user did not name.
- Technicians are TechBldrs staff; tickets are assigned to them. Client people (requestors, contacts) are not technicians.
- Ticket stages: open (not archived, category not 9 REVIEW), review (category 9 REVIEW), archived, live (open + review). "Open" means stage=open. Leave archived out unless the user asks for it.
- Counts, hour totals and "which has the most" come from ticket_stats. Never count or add up rows yourself.
- Flow mail is mail filed on tickets, not a live mailbox."""
