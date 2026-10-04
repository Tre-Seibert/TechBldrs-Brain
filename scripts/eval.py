"""Score tb-brain against eval/questions.json, and compare two runs.

Run against a live tb-brain (FLOW_MODE=stub, so the expected answers hold):

    python scripts/eval.py run --label qwen2.5-routers-on
    python scripts/eval.py run --label qwen3-routers-off --group longest_time
    python scripts/eval.py compare eval/results/A.json eval/results/B.json

Each run records the model and DISABLED_ROUTERS the server reports on /health, so a result
file says what it measured. Change those with LLM_MODEL / DISABLED_ROUTERS in .env, restart
tb-brain, then run again.
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


def _reply_text(payload: dict) -> str:
    choices = payload.get("choices") or [{}]
    return str((choices[0].get("message") or {}).get("content") or "")


def run(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.questions), args.group, args.only)
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2
    base = args.url.rstrip("/")
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
            try:
                response = client.post(
                    f"{base}/v1/chat/completions",
                    json={"model": "tb-brain", "messages": case["messages"], "stream": False},
                )
                response.raise_for_status()
                reply = _reply_text(response.json())
                failures = check_reply(case, reply)
            except httpx.HTTPError as exc:
                reply, failures = "", [f"request failed: {exc}"]
            seconds = round(time.perf_counter() - started, 2)
            status = "PASS" if not failures else "FAIL"
            print(f"{status}  {case['group']:<17} {case['id']:<22} {seconds:>6.1f}s")
            for failure in failures:
                print(f"        - {failure}")
            results.append(
                {"id": case["id"], "group": case["group"], "pass": not failures,
                 "failures": failures, "seconds": seconds, "reply": reply}
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
    run_p.set_defaults(func=run)
    cmp_p = sub.add_parser("compare", help="diff two result files")
    cmp_p.add_argument("a")
    cmp_p.add_argument("b")
    cmp_p.set_defaults(func=compare)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
