"""Command-line interface.

    kubesleuth scenarios
    kubesleuth investigate --scenario oom-kill                # offline playbook brain
    kubesleuth investigate --scenario oom-kill --brain claude # Claude brain
    kubesleuth eval --brain rules                             # score against ground truth
    kubesleuth mcp --scenario oom-kill                        # MCP server
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from typing import Any

from .agent import Investigation, approve_all, deny_all
from .cluster.simulated import SimulatedCluster, list_scenarios, load_scenario
from .models import Event, Proposal

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


DIM, BOLD, BLUE, GREEN, YELLOW, RED, MAGENTA = "2", "1", "34", "32", "33", "31", "35"


def _wrap(text: str, indent: str = "   ") -> str:
    return "\n".join(textwrap.fill(p, width=96, initial_indent=indent, subsequent_indent=indent) for p in text.split("\n") if p.strip())


def _short(content: str, limit: int = 260) -> str:
    flat = " ".join(content.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def print_event(event: Event) -> None:
    d = event.data
    k = event.kind
    if k == "alert":
        print(_c(RED + ";" + BOLD, f"\n▲ {d['name']}") + _c(DIM, f"  [{d.get('severity')}]  {d['target']} in namespace {d['namespace']}"))
        print(_wrap(d["summary"]))
    elif k == "thought":
        print(_c(MAGENTA, "\n◆ thinking"))
        print(_wrap(d["text"]))
    elif k == "tool_call":
        args = ", ".join(f"{a}={json.dumps(v)}" for a, v in d["input"].items() if a not in ("evidence", "follow_ups", "root_cause", "rationale"))
        print(_c(BLUE, f"  → {d['name']}") + _c(DIM, f"({args})"))
    elif k == "tool_result":
        if d["name"] in ("submit_report", "propose_remediation"):
            return
        color = RED if d["is_error"] else DIM
        print(_c(color, "    " + _short(d["content"])))
    elif k == "approval_request":
        print(_c(YELLOW + ";" + BOLD, f"\n⚑ Approval needed: {d['summary']}") + _c(DIM, f"  risk={d['risk']}"))
        print(_wrap(d["rationale"]))
    elif k == "approval":
        print(_c(GREEN if d["approved"] else YELLOW, f"  {'✓ approved' if d['approved'] else '✗ rejected'}: {d['summary']}"))
    elif k == "action_result":
        print(_c(GREEN if d.get("symptoms_resolved") else YELLOW, f"  {d.get('summary', '')}"))
        for check in d.get("verification", []):
            print(_c(DIM, f"    · {check}"))
    elif k == "error":
        print(_c(RED, f"  ! {d['message']}"))


def print_report(report: Any) -> None:
    status_color = {"resolved": GREEN, "awaiting_human": YELLOW}.get(report.status, BLUE)
    print(_c(BOLD, "\n━━ Incident report " + "━" * 60))
    print(f"  status      {_c(status_color, report.status.replace('_', ' '))}")
    print(f"  category    {report.category}")
    print(f"  confidence  {round(report.confidence * 100)}%")
    print(_c(BOLD, "\n  Root cause"))
    print(_wrap(report.root_cause))
    print(_c(BOLD, "\n  Evidence"))
    for e in report.evidence:
        print(_wrap("• " + e))
    print(_c(BOLD, "\n  Remediation"))
    print(_wrap(report.remediation))
    if report.follow_ups:
        print(_c(BOLD, "\n  Follow-ups"))
        for f in report.follow_ups:
            print(_wrap("• " + f))
    print()


def ask_human(proposal: Proposal) -> bool:
    try:
        answer = input(_c(YELLOW, f"  Apply '{proposal.describe()}'? [y/N] ")).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def make_brain(args: argparse.Namespace):
    if args.brain == "claude":
        from .brains.claude import ClaudeBrain
        return ClaudeBrain(model=args.model, effort=args.effort)
    from .brains.rules import RuleBrain
    return RuleBrain()


def make_backend(args: argparse.Namespace):
    if getattr(args, "live", False):
        from .cluster.live import LiveCluster
        if not (args.namespace and args.target):
            sys.exit("--live needs --namespace and --target (for example --target deployment/checkout-api)")
        alert = {
            "name": args.alert_name, "severity": "unknown", "namespace": args.namespace,
            "target": args.target, "summary": args.summary or f"Investigate {args.target} in {args.namespace}.",
        }
        return LiveCluster(alert, prometheus_url=args.prometheus, allow_writes=args.allow_writes)
    return SimulatedCluster(args.scenario)


def cmd_scenarios(_: argparse.Namespace) -> int:
    for s in list_scenarios():
        print(f"  {s['id']:<18} {s['category']:<17} {s['title']}")
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    approver = {"ask": ask_human, "yes": approve_all, "no": deny_all}[args.approve]
    inv = Investigation(make_backend(args), make_brain(args), approver=approver, listener=None if args.quiet else print_event)
    report = inv.run()
    print_report(report)
    if args.trace:
        with open(args.trace, "w", encoding="utf-8") as f:
            json.dump(inv.trace(), f, indent=1, default=str)
        print(_c(DIM, f"  trace written to {args.trace}"))
    usage = getattr(inv.brain, "usage", None)
    if usage:
        print(_c(DIM, f"  tokens: {usage['input_tokens']} in, {usage['output_tokens']} out, {usage['cache_read_input_tokens']} from cache"))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    rows = []
    for s in list_scenarios():
        truth = load_scenario(s["id"])["ground_truth"]
        backend = SimulatedCluster(s["id"])
        args.scenario = s["id"]
        inv = Investigation(backend, make_brain(args), approver=approve_all)
        report = inv.run()
        proposed = inv.proposal.action if inv.proposal else "-"
        rows.append({
            "scenario": s["id"], "expected": truth["category"], "got": report.category,
            "category_ok": report.category == truth["category"], "action": proposed,
            "resolved": backend.resolved, "tool_calls": inv.tool_calls,
        })
    width = max(len(r["scenario"]) for r in rows)
    print(f"\n  {'scenario':<{width}}  {'expected':<17} {'diagnosed':<19} {'fix':<20} {'resolved':<9} calls")
    for r in rows:
        ok = _c(GREEN, "✓") if r["category_ok"] else _c(RED, "✗")
        res = _c(GREEN, "yes") if r["resolved"] else _c(RED, "no ")
        print(f"  {r['scenario']:<{width}}  {r['expected']:<17} {ok} {r['got']:<17} {r['action']:<20} {res:<9} {r['tool_calls']}")
    diag = sum(r["category_ok"] for r in rows)
    fixed = sum(r["resolved"] for r in rows)
    print(f"\n  diagnosis accuracy {diag}/{len(rows)} · incidents resolved {fixed}/{len(rows)} · "
          f"avg tool calls {sum(r['tool_calls'] for r in rows) / len(rows):.1f}\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1)
    return 0 if diag == len(rows) else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve
    serve(make_backend(args))
    return 0


def _add_backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--scenario", default="oom-kill", help="simulated incident to replay (see `kubesleuth scenarios`)")
    p.add_argument("--live", action="store_true", help="use the current kubeconfig context instead of a scenario")
    p.add_argument("--namespace", help="live: namespace of the alerting workload")
    p.add_argument("--target", help="live: workload, e.g. deployment/checkout-api")
    p.add_argument("--alert-name", default="ManualInvestigation", help="live: alert name to show the agent")
    p.add_argument("--summary", help="live: one-line description of the symptom")
    p.add_argument("--prometheus", help="live: Prometheus base URL (default: $PROMETHEUS_URL)")
    p.add_argument("--allow-writes", action="store_true", help="live: let approved remediations change the cluster")


def _add_brain_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--brain", choices=["rules", "claude"], default="rules", help="rules runs offline; claude needs an API key")
    p.add_argument("--model", default="claude-opus-5-5", help="Claude model ID")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="kubesleuth", description="Agentic SRE for Kubernetes incidents.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="list bundled incident scenarios").set_defaults(func=cmd_scenarios)

    p = sub.add_parser("investigate", help="investigate an incident")
    _add_backend_args(p)
    _add_brain_args(p)
    p.add_argument("--approve", choices=["ask", "yes", "no"], default="ask", help="how to answer the approval gate")
    p.add_argument("--trace", help="write a JSON trace of the investigation to this file")
    p.add_argument("--quiet", action="store_true", help="only print the final report")
    p.set_defaults(func=cmd_investigate)

    p = sub.add_parser("eval", help="run every scenario and score diagnoses against ground truth")
    _add_brain_args(p)
    p.add_argument("--json", help="write per-scenario results to this file")
    p.set_defaults(func=cmd_eval, live=False)

    p = sub.add_parser("mcp", help="serve the read-only tools over MCP (stdio)")
    _add_backend_args(p)
    p.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
