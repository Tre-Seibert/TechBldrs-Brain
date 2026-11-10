"""tb-brain's MCP endpoint: the read-only tools of every enabled source, served to Copilot Studio.

Streamable HTTP (the official `mcp` SDK, stateless, JSON responses) at /mcp (every enabled source) and
/mcp/<source> (one source), registered on the FastAPI app in app/main.py. Each call runs through `run_tool`, so
validation, the audit row and the sources' own error wording are the same as in the chat loop. The caller is a
technician signed in through Microsoft Entra (app/mcp/auth.py); their UPN is the audit actor and the identity Flow
sees. The write tool (merge_tickets) and search_knowledge are not offered.
"""
