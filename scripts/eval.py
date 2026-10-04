"""Score tb-brain against eval/questions.json, and compare two runs.

Run against a live tb-brain (FLOW_MODE=stub, so the expected answers hold):

    python scripts/eval.py run --label qwen2.5-routers-on
    python scripts/eval.py run --questions eval/flow_cases.json --label q4-flow
    python scripts/eval.py run --label qwen3-routers-off --group longest_time
    python scripts/eval.py compare eval/results/A.json eval/results/B.json

Each run records the model and DISABLED_ROUTERS the server reports on /health, so a result
file says what it measured. Change those with LLM_MODEL / DISABLED_ROUTERS in .env, restart
tb-brain, then run again.

Besides reply text (must_contain / must_not_contain / must_match / must_not_match), a case may
score the tool calls the server made (the server reports them in the response's x_tb_brain):

    "expect":       [spec, {"any": [spec, spec]}]   every entry must be satisfied
    "forbid_calls": [spec]                         none may appear
    "expect_no_calls": true                        no tool and no router at all
    "routers_ok":   ["longest_time"]               a deterministic router answering counts as
                                                   satisfying "expect" (it skips the model)

    spec = {"tool": "list_tickets" | ["a", "b"], "args": {"stage": "open", "assignee_code": ["me", "ts"],
            "unassigned": true, "client_code": "*", "after": {"re": "^\\d{4}-\\d{2}-\\d{2}"}}}

An arg value matches when it equals the expected one (strings case-insensitive), is one of a
list, is non-empty for "*", or matches {"re": ...}. Run as a signed-in technician with --as-email.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "eval" / "questions.json"
RESULTS = ROOT / "eval" / "results"


def load_cases(path: Path = QUESTIONS, group: str | None = None, only: list[str] | None = None) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if group:
        cases = [case for case in cases if case["group"] == group]
    if only:
        cases = [case for case in cases if case["id"] in only]
    return cases


def check_reply(case: dict, reply: str) -> list[str]:
    """Return the failed checks for one reply (empty means pass)."""
    failures: list[str] = []
    lowered = reply.lower()
    for needle in case.get("must_contain", []):
        if needle.lower() not in lowered:
            failures.append(f"missing {needle!r}")
    for needle in case.get("must_not_contain", []):
        if needle.lower() in lowered:
            failures.append(f"should not contain {needle!r}")
    for pattern in case.get("must_match", []):
        if not re.search(pattern, reply):
            failures.append(f"no match for /{pattern}/")
    for pattern in case.get("must_not_match", []):
        found = re.search(pattern, reply)
        if found:
            failures.append(f"unexpected match {found.group(0)!r}")
    return failures


def _arg_matches(actual, wanted) -> bool:
    if wanted == "*":
        return actual not in (None, "", [], {})
    if isinstance(wanted, dict) and "re" in wanted:
        return actual is not None and re.search(wanted["re"], str(actual)) is not None
    if isinstance(wanted, list):
        return any(_arg_matches(actual, item) for item in wanted)
    if isinstance(wanted, bool):
        return isinstance(actual, bool) and actual is wanted
    if isinstance(wanted, str):
        return str(actual).strip().lower() == wanted.strip().lower()
    return actual == wanted


def call_matches(call: dict, spec: dict) -> bool:
    tools = spec.get("tool")
    names = [tools] if isinstance(tools, str) else list(tools or [])
    if names and call.get("name") not in names:
        return False
    arguments = call.get("arguments") or {}
    return all(_arg_matches(arguments.get(key), wanted) for key, wanted in (spec.get("args") or {}).items())


def _describe(spec: dict) -> str:
    tools = spec.get("tool")
    name = "/".join([tools] if isinstance(tools, str) else tools or ["*"])
    return f"{name}({json.dumps(spec.get('args') or {}, separators=(',', ':'))})"


def check_calls(case: dict, trace: dict | None) -> list[str]:
    """Failed tool-call checks for one case. `trace` is the server's x_tb_brain block."""
    wants = case.get("expect") or []
    forbids = case.get("forbid_calls") or []
    if not (wants or forbids or case.get("expect_no_calls")):
        return []
    trace = trace or {}
    calls = trace.get("tool_calls") or []
    router = trace.get("router")
    failures: list[str] = []
    if case.get("expect_no_calls") and (calls or router):
        names = [c.get("name") for c in calls] or [f"router:{router}"]
        failures.append(f"expected no tool calls, got {', '.join(names)}")
    for spec in forbids:
        hit = next((c for c in calls if call_matches(c, spec)), None)
        if hit:
            failures.append(f"forbidden call {_describe(spec)}")
    if router and router in (case.get("routers_ok") or []):
        return failures
    for want in wants:
        options = want["any"] if "any" in want else [want]
        if not any(call_matches(c, opt) for c in calls for opt in options):
            wording = " or ".join(_describe(opt) for opt in options)
            failures.append(f"no call matching {wording}")
    return failures


def _calls_summary(trace: dict | None) -> str:
    trace = trace or {}
    if trace.get("router"):
        return f"router:{trace['router']}"
    parts = []
    for call in trace.get("tool_calls") or []:
        args = json.dumps(call.get("arguments") or {}, separators=(",", ":"))
        parts.append(f"{call.get('name')}({args}){'' if call.get('ok', True) else ' [ERR]'}")
    return "; ".join(parts) or "(no tool calls)"


def _reply_text(payload: dict) -> str:
    choices = payload.get("choices") or [{}]
    return str((choices[0].get("message") or {}).get("content") or "")


def run(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.questions), args.group, args.only)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
    headers = {"X-OpenWebUI-User-Email": args.as_email} if args.as_email else {}
    results: list[dict] = []
    with httpx.Client(timeout=args.timeout) as client:
        health = client.get(f"{base}/health").json()
        if health.get("flow_source") != "stub":
            print(
                f"warning: server reports flow_source={health.get('flow_source')!r}; "
                "expected answers assume the stub fixtures",
                file=sys.stderr,
            )
        print(
            f"model={health.get('llm_model') or '(ollama default)'} "
            f"disabled_routers={health.get('disabled_routers')} cases={len(cases)}\n"
        )
        for case in cases:
            started = time.perf_counter()
            trace = None
            try:
                response = client.post(
                    f"{base}/v1/chat/completions",
                    json={"model": "tb-brain", "messages": case["messages"], "stream": False},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                reply = _reply_text(payload)
                trace = payload.get("x_tb_brain")
                failures = check_reply(case, reply) + check_calls(case, trace)
            except httpx.HTTPError as exc:
                reply, failures = "", [f"request failed: {exc}"]
            seconds = round(time.perf_counter() - started, 2)
            status = "PASS" if not failures else "FAIL"
            print(f"{status}  {case['group']:<17} {case['id']:<22} {seconds:>6.1f}s")
            for failure in failures:
                print(f"        - {failure}")
            if failures or args.verbose:
                print(f"        calls: {_calls_summary(trace)}")
            results.append(
                {"id": case["id"], "group": case["group"], "pass": not failures,
                 "failures": failures, "seconds": seconds, "reply": reply,
                 "calls": (trace or {}).get("tool_calls", []), "router": (trace or {}).get("router")}
            )
    summary = summarize(results)
    print("\n" + format_summary(summary))
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"{stamp}-{args.label}.json"
    out.write_text(
        json.dumps(
            {"label": args.label, "when": stamp, "model": health.get("llm_model"),
             "disabled_routers": health.get("disabled_routers"), "summary": summary, "results": results},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nsaved {out.relative_to(ROOT)}")
    return 0


def summarize(results: list[dict]) -> dict:
    groups: dict[str, list[int]] = {}
    for item in results:
        passed, total = groups.setdefault(item["group"], [0, 0])
        groups[item["group"]] = [passed + int(item["pass"]), total + 1]
    return {
        "passed": sum(1 for item in results if item["pass"]),
        "total": len(results),
        "groups": groups,
        "avg_seconds": round(sum(item["seconds"] for item in results) / max(len(results), 1), 2),
    }


def format_summary(summary: dict) -> str:
    lines = [f"{summary['passed']}/{summary['total']} passed, avg {summary['avg_seconds']}s per question"]
    for name, (passed, total) in sorted(summary["groups"].items()):
        lines.append(f"  {name:<17} {passed}/{total}")
    return "\n".join(lines)


def compare(args: argparse.Namespace) -> int:
    first = json.loads(Path(args.a).read_text(encoding="utf-8"))
    second = json.loads(Path(args.b).read_text(encoding="utf-8"))
    for tag, data in (("A", first), ("B", second)):
        print(
            f"{tag}: {data['label']}  model={data.get('model')}  "
            f"disabled_routers={data.get('disabled_routers')}\n   {format_summary(data['summary']).splitlines()[0]}"
        )
    before = {item["id"]: item for item in first["results"]}
    after = {item["id"]: item for item in second["results"]}
    fixed = [i for i in after if i in before and not before[i]["pass"] and after[i]["pass"]]
    broken = [i for i in after if i in before and before[i]["pass"] and not after[i]["pass"]]
    print(f"\nB fixed {len(fixed)}: {', '.join(fixed) or '-'}")
    print(f"B broke {len(broken)}: {', '.join(broken) or '-'}")
    for case_id in broken:
        print(f"\n{case_id} (B reply):\n  {after[case_id]['reply'][:600]!r}")
        for failure in after[case_id]["failures"]:
            print(f"  - {failure}")
    return 1 if broken else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="score a live tb-brain")
    run_p.add_argument("--url", default="http://127.0.0.1:8765")
    run_p.add_argument("--label", default="run", help="short name saved in the result file")
    run_p.add_argument("--group", help="only this group (a router name, or 'tools')")
    run_p.add_argument("--only", nargs="+", help="only these case ids")
    run_p.add_argument("--questions", default=str(QUESTIONS))
    run_p.add_argument("--timeout", type=float, default=600.0)
    run_p.add_argument(
        "--as-email",
        default="tseibert@techbldrs.example",
        help="signed-in technician for 'me' (stub fixtures: Tre = ts); empty string sends no identity",
    )
    run_p.add_argument("--verbose", action="store_true", help="print the tool calls for passing cases too")
    run_p.set_defaults(func=run)
    cmp_p = sub.add_parser("compare", help="diff two result files")
    cmp_p.add_argument("a")
    cmp_p.add_argument("b")
    cmp_p.set_defaults(func=compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
