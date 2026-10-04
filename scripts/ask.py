"""Ask tb-brain questions from the terminal (no Open WebUI needed).

    python scripts/ask.py                      # interactive; remembers the conversation
    python scripts/ask.py "What are my open tickets?"   # one question

Needs tb-brain running (.\scripts\run.ps1 in another window). "me" is resolved from --as-email
(default: Tre's real address on FLOW_MODE=http, the stub technician on FLOW_MODE=stub). If the
server has BRAIN_USER_JWT_SECRET set, a signed identity is sent using it from the environment or .env.
Type /new to start over, /quit to leave. The line under each answer shows which tools ran.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
REAL_EMAIL = "tseibert@techbldrs.com"
STUB_EMAIL = "tseibert@techbldrs.example"


def _eval_module():
    spec = importlib.util.spec_from_file_location("tb_eval", ROOT / "scripts" / "eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calls_line(trace: dict | None) -> str:
    trace = trace or {}
    if trace.get("router"):
        return f"[answered by router: {trace['router']}]"
    names = [
        f"{c.get('name')}{'' if c.get('ok', True) else ' (error)'}" for c in trace.get("tool_calls") or []
    ]
    return f"[tools: {', '.join(names)}]" if names else "[no tools]"


def ask(client: httpx.Client, base: str, headers: dict, messages: list[dict]) -> tuple[str, dict | None]:
    response = client.post(
        f"{base}/v1/chat/completions",
        json={"model": "tb-brain", "messages": messages, "stream": False},
        headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    reply = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return reply, payload.get("x_tb_brain")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="*", help="ask once and exit")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--as-email", default=None, help="who 'me' is")
    parser.add_argument("--jwt-secret", default=None)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    evalmod = _eval_module()
    with httpx.Client(timeout=600.0) as client:
        try:
            health = client.get(f"{base}/health").json()
        except httpx.HTTPError as exc:
            print(f"tb-brain is not reachable at {base}: {exc}\nStart it with .\scripts\run.ps1", file=sys.stderr)
            return 2
        stub = health.get("flow_source") == "stub"
        email = args.as_email if args.as_email is not None else (STUB_EMAIL if stub else REAL_EMAIL)
        secret = args.jwt_secret if args.jwt_secret is not None else evalmod._env_value("BRAIN_USER_JWT_SECRET")
        headers = evalmod.identity_headers(email, secret)
        print(
            f"tb-brain {health.get('version')} | data: {health.get('flow_source')} | model: "
            f"{health.get('llm_model') or '(ollama default)'} | you: {email or '(none)'}"
        )
        if stub:
            print("NOTE: stub data (fake tickets), not real Flow.")
        messages: list[dict] = []
        pending = [" ".join(args.question)] if args.question else []
        while True:
            if pending:
                text = pending.pop(0)
            else:
                try:
                    text = input("\nyou> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
            if not text:
                continue
            if text in ("/quit", "/exit"):
                return 0
            if text == "/new":
                messages = []
                print("(new conversation)")
                continue
            messages.append({"role": "user", "content": text})
            try:
                reply, trace = ask(client, base, headers, messages)
            except httpx.HTTPError as exc:
                print(f"request failed: {exc}", file=sys.stderr)
                messages.pop()
                if args.question:
                    return 1
                continue
            messages.append({"role": "assistant", "content": reply})
            print(f"\n{reply}\n\n{_calls_line(trace)}")
            if args.question:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
