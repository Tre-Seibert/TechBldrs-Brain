"""Check whether Ollama reuses its prompt cache across signed-in users (PROMPT_STYLE legacy vs lean).

    python scripts/probe_cache.py --style legacy
    python scripts/probe_cache.py --style lean
    python scripts/probe_cache.py --style lean --model qwen3:14b --question "What are my open tickets?"

Builds the exact messages and tools tb-brain would send (same code path as app/agent/loop.py, stub Flow for
who is signed in) and sends the same question three times to Ollama's native /api/chat at LLM_BASE_URL
without /v1: as user A, as user B, then as user B again. Ollama reports prompt_eval_count and
prompt_eval_duration only on its native API, and counts only the tokens it had to read, not the ones it
reused from cache. Only one token is generated per call.

What to look for: the B call after A. In legacy the signed-in line sits in the system message ahead of the
tool list, so B re-reads nearly the whole prompt. In lean only the session context and question change, so B
should read a few dozen tokens. B again should read almost nothing in both styles.

It also prints where the model's template puts the system text relative to the tool list (from /api/show).
Run it with nothing else using the model, or the other requests will evict the cache between calls.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.loop import _ensure_system  # noqa: E402
from app.config import PROMPT_STYLES, Settings  # noqa: E402
from app.identity import current_signed_in_email  # noqa: E402
from app.sources.registry import build_sources  # noqa: E402
from app.tools import openai_tools  # noqa: E402

QUESTION = "What are my open tickets?"
# Stub Flow technicians (app/sources/flow/fixtures.py): Tre Seibert (ts) and Tom Rivera (tr).
USER_A = "tseibert@techbldrs.example"
USER_B = "trivera@techbldrs.example"


def native_base(llm_base_url: str) -> str:
    base = llm_base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def build_request(settings: Settings, sources, style: str, email: str, question: str, model: str) -> dict:
    token = current_signed_in_email.set(email)
    try:
        messages = _ensure_system(
            [{"role": "user", "content": question}], source=sources, no_think=settings.llm_no_think, style=style
        )
    finally:
        current_signed_in_email.reset(token)
    body = {
        "model": model,
        "messages": messages,
        "tools": openai_tools(sources, style=style),
        "stream": False,
        "options": {"num_predict": 1, "temperature": 0},
    }
    if "think" in settings.llm_extra_body_dict:
        body["think"] = settings.llm_extra_body_dict["think"]
    return body


def template_report(client: httpx.Client, model: str) -> str:
    try:
        response = client.post("/api/show", json={"model": model})
        response.raise_for_status()
        template = str(response.json().get("template") or "")
    except (httpx.HTTPError, ValueError) as exc:
        return f"template: could not read it ({exc})"
    system_at = template.find(".System")
    tools_at = template.find(".Tools")
    order = "system text before the tool list" if 0 <= system_at < tools_at else "check by hand"
    system_branch = 'eq .Role "system"' in template
    return (
        f"template: {order}; system messages inside the message loop: "
        f"{'rendered in place' if system_branch else 'not rendered (joined into .System)'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--style", choices=PROMPT_STYLES, help="default: PROMPT_STYLE from .env")
    parser.add_argument("--model", help="default: LLM_MODEL from .env")
    parser.add_argument("--url", help="default: LLM_BASE_URL from .env (the /v1 suffix is dropped)")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--user-a", default=USER_A)
    parser.add_argument("--user-b", default=USER_B)
    args = parser.parse_args()

    settings = Settings()
    style = args.style or settings.prompt_style
    model = args.model or settings.llm_model.strip()
    if not model:
        print("set --model or LLM_MODEL", file=sys.stderr)
        return 2
    # Stub Flow only resolves who is signed in; nothing here calls real Flow.
    sources = build_sources(settings.model_copy(update={"flow_mode": "stub"}))
    base = native_base(args.url or settings.llm_base_url)
    runs = [("A", args.user_a), ("B", args.user_b), ("B again", args.user_b)]
    with httpx.Client(base_url=base, timeout=settings.llm_timeout_seconds) as client:
        print(f"style={style} model={model} url={base}/api/chat")
        print(template_report(client, model))
        print(f"{'call':<9} {'user':<30} {'prompt_eval_count':>18} {'prompt_eval_ms':>15}")
        for label, email in runs:
            body = build_request(settings, sources, style, email, args.question, model)
            response = client.post("/api/chat", json=body)
            if response.status_code >= 400:
                print(f"{label:<9} HTTP {response.status_code}: {response.text[:200]}")
                return 1
            data = response.json()
            count = data.get("prompt_eval_count")
            millis = (data.get("prompt_eval_duration") or 0) / 1e6
            print(f"{label:<9} {email:<30} {count!s:>18} {millis:>15.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
