"""Find out which request shape actually turns thinking off for a model served by Ollama.

    python scripts/probe_think.py --model qwen3:14b

Sends the same tiny question four ways and prints seconds, completion tokens and whether the
reply carried reasoning. Thinking inflates completion tokens and seconds. Put the winner in .env
(LLM_NO_THINK=true for the /no_think switch, or LLM_EXTRA_BODY={"..."} for a request field).
"""

from __future__ import annotations

import argparse
import time

import httpx

QUESTION = "A client emailed twice today. In one short sentence, what should a help desk tech do first?"


def probe(client: httpx.Client, model: str, label: str, system: str | None, extra: dict) -> None:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": QUESTION}
    ]
    body = {"model": model, "messages": messages, "stream": False, **extra}
    started = time.perf_counter()
    response = client.post("/chat/completions", json=body)
    seconds = time.perf_counter() - started
    if response.status_code >= 400:
        print(f"{label:<28} HTTP {response.status_code}: {response.text[:120]}")
        return
    data = response.json()
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
    tokens = (data.get("usage") or {}).get("completion_tokens")
    thought = bool(reasoning) or "<think>" in content
    print(f"{label:<28} {seconds:5.1f}s  completion_tokens={tokens}  reasoning={'yes' if thought else 'no'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=300.0) as client:
        probe(client, args.model, "baseline", None, {})
        probe(client, args.model, "system /no_think", "/no_think", {})
        probe(client, args.model, 'reasoning_effort="none"', None, {"reasoning_effort": "none"})
        probe(client, args.model, "think=false", None, {"think": False})
        probe(client, args.model, "baseline (again)", None, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
