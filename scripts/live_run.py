r"""Ask every question in eval/live_questions.txt against a running tb-brain and save the answers.

    python scripts/live_run.py                       # all questions, saves eval/results/live-<time>.md
    python scripts/live_run.py --only BUCK           # only questions containing "BUCK"
    python scripts/live_run.py --label after-fix     # name the output file live-after-fix.md
    python scripts/live_run.py --url http://127.0.0.1:8765 --as-email tseibert@techbldrs.com

Needs tb-brain running (.\scripts\run.ps1) with FLOW_MODE=http, so the answers come from real Flow.
Nothing here writes to Flow: the question list only asks for merge *suggestions*. "me" is resolved
the same way scripts/ask.py does it (signed identity when BRAIN_USER_JWT_SECRET is set).

The output file has, for every question: the reply, which tools ran (or which router answered),
the arguments of each tool call, and how long it took. Send that file back to have the answers
checked against the production database.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "live_questions.txt"
RESULTS = ROOT / "eval" / "results"
REAL_EMAIL = "tseibert@techbldrs.com"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_conversations(path: Path = QUESTIONS, only: str | None = None) -> list[list[str]]:
    """Blocks of questions separated by blank lines; '#' lines are comments."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    if only:
        needle = only.lower()
        blocks = [block for block in blocks if any(needle in turn.lower() for turn in block)]
    return blocks


def format_calls(trace: dict | None) -> list[str]:
    trace = trace or {}
    if trace.get("router"):
        return [f"answered by router: {trace['router']}"]
    lines = []
    for call in trace.get("tool_calls") or []:
        status = "" if call.get("ok", True) else f"  ERROR: {call.get('error')}"
        lines.append(f"{call.get('name')}({json.dumps(call.get('arguments') or {}, sort_keys=True)}){status}")
    return lines or ["no tools called"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--questions", default=str(QUESTIONS))
    parser.add_argument("--only", default=None, help="only conversations containing this text")
    parser.add_argument("--label", default=None)
    parser.add_argument("--as-email", default=REAL_EMAIL, help="who 'me' is")
    parser.add_argument("--jwt-secret", default=None)
    parser.add_argument("--allow-stub", action="store_true", help="run even if the server is on stub data")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for each answer")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    ask_mod, eval_mod = _load("ask"), _load("eval")
    conversations = load_conversations(Path(args.questions), args.only)
    if not conversations:
        print("no questions matched", file=sys.stderr)
        return 2
    total = sum(len(block) for block in conversations)

    with httpx.Client(timeout=args.timeout) as client:
        try:
            health = client.get(f"{base}/health").json()
        except httpx.HTTPError as exc:
            print(f"tb-brain is not reachable at {base}: {exc}\nStart it with .\\scripts\\run.ps1", file=sys.stderr)
            return 2
        if health.get("flow_source") == "stub" and not args.allow_stub:
            print("tb-brain is on stub data (FLOW_MODE=stub). Set FLOW_MODE=http and restart, or pass --allow-stub.", file=sys.stderr)
            return 2
        secret = args.jwt_secret if args.jwt_secret is not None else eval_mod._env_value("BRAIN_USER_JWT_SECRET")
        headers = eval_mod.identity_headers(args.as_email, secret)

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        RESULTS.mkdir(parents=True, exist_ok=True)
        name = f"live-{args.label}" if args.label else f"live-{stamp}"
        md_path, json_path = RESULTS / f"{name}.md", RESULTS / f"{name}.json"
        header = (
            f"# tb-brain live run {stamp}\n\n"
            f"- server: {base} | version {health.get('version')} | data: {health.get('flow_source')}\n"
            f"- model: {health.get('llm_model') or '(ollama default)'} | disabled routers: {health.get('disabled_routers')}\n"
            f"- asked as: {args.as_email} | questions: {total}\n"
        )
        print(header)

        records: list[dict] = []
        out = [header]
        number = 0
        for block in conversations:
            messages: list[dict] = []
            for question in block:
                number += 1
                messages.append({"role": "user", "content": question})
                started = time.time()
                try:
                    reply, trace = ask_mod.ask(client, base, headers, messages)
                    error = None
                except httpx.HTTPError as exc:
                    reply, trace, error = "", None, str(exc)
                seconds = time.time() - started
                messages.append({"role": "assistant", "content": reply})
                calls = format_calls(trace)
                print(f"[{number}/{total}] {seconds:5.1f}s  {question}")
                records.append(
                    {"question": question, "reply": reply, "calls": calls, "seconds": round(seconds, 1), "error": error,
                     "x_tb_brain": trace}
                )
                out.append(f"\n## {number}. {question}\n")
                out.append(f"_{seconds:.1f}s_\n")
                out.append("**Reply**\n\n" + (reply.strip() or f"(no reply{': ' + error if error else ''})") + "\n")
                out.append("**Tool calls**\n\n" + "\n".join(f"- `{line}`" for line in calls) + "\n")
                md_path.write_text("\n".join(out), encoding="utf-8")  # saved as it goes, so a stop loses nothing
        json_path.write_text(
            json.dumps({"stamp": stamp, "health": health, "as": args.as_email, "results": records}, indent=2),
            encoding="utf-8",
        )
    slow = sorted(records, key=lambda r: -r["seconds"])[:3]
    print(f"\nDone. {len(records)} answers in {sum(r['seconds'] for r in records):.0f}s.")
    print("Slowest: " + "; ".join(f"{r['seconds']}s {r['question']}" for r in slow))
    print(f"Saved: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
