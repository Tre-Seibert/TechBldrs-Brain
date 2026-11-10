r"""Call tb-brain's MCP endpoint directly, without Copilot Studio.

    python scripts/mcp_smoke.py --url http://127.0.0.1:8765/mcp --api-key "$MCP_API_KEY" list
    python scripts/mcp_smoke.py --url https://brain.techbldrs.dev/mcp --token "$ENTRA_ACCESS_TOKEN" list
    python scripts/mcp_smoke.py --url https://brain.techbldrs.dev/mcp/datto --api-key ... call datto_list_devices client_code=WDON
    python scripts/mcp_smoke.py --url ... --api-key ... init

Plain JSON-RPC over Streamable HTTP, the way Copilot Studio talks to the server: one POST per request with
`Accept: application/json, text/event-stream`. `list` prints every tool with the first line of its description,
`call` prints the text the agent's model would see, `init` prints the server's initialize reply. Argument values
are parsed as JSON when they look like it (limit=5, online=false), else kept as strings.

The token or key is read from --api-key / --token or the MCP_API_KEY / MCP_TOKEN environment variables; it is never
printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _arguments(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"argument {pair!r} is not key=value")
        out[key.strip()] = _value(value)
    return out


def _parse_body(response: httpx.Response) -> dict[str, Any]:
    """JSON, or the last JSON-RPC message of an SSE body (if the server streamed anyway)."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        last: dict[str, Any] | None = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    last = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return last or {}
    return response.json()


def rpc(client: httpx.Client, url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text[:500]}", file=sys.stderr)
        raise SystemExit(2)
    body = _parse_body(response)
    if "error" in body:
        print(f"JSON-RPC error: {json.dumps(body['error'])}", file=sys.stderr)
        raise SystemExit(3)
    return body.get("result") or {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="the MCP endpoint, e.g. https://brain.techbldrs.dev/mcp")
    parser.add_argument("--api-key", default=os.environ.get("MCP_API_KEY", ""), help="MCP_AUTH=api_key: the shared key")
    parser.add_argument("--token", default=os.environ.get("MCP_TOKEN", ""), help="MCP_AUTH=entra: an access token for the app")
    parser.add_argument("--timeout", type=float, default=120.0)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="print the initialize reply")
    sub.add_parser("list", help="print the tools offered")
    call = sub.add_parser("call", help="call one tool: call <name> key=value ...")
    call.add_argument("name")
    call.add_argument("pairs", nargs="*")
    args = parser.parse_args(argv)

    headers = dict(HEADERS)
    secret = args.token or args.api_key
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    with httpx.Client(headers=headers, timeout=args.timeout) as client:
        if args.command == "init":
            result = rpc(client, args.url, "initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "tb-brain-smoke", "version": "1"},
            })
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "list":
            tools = rpc(client, args.url, "tools/list").get("tools", [])
            for tool in tools:
                first = (tool.get("description") or "").strip().splitlines()[:1]
                print(f"- {tool['name']}: {first[0] if first else ''}"[:160])
            print(f"{len(tools)} tool(s)")
            return 0
        result = rpc(client, args.url, "tools/call", {"name": args.name, "arguments": _arguments(args.pairs)})
        if result.get("isError"):
            print("[isError]")
        for block in result.get("content", []):
            if block.get("type") == "text":
                print(block["text"])
            else:
                print(json.dumps(block))
        return 1 if result.get("isError") else 0


if __name__ == "__main__":
    raise SystemExit(main())
