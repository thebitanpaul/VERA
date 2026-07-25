"""Command-line front-end.

    python -m vera "Is concomitant metformin permitted?" --trial 412

Exit codes: ``0`` answered, ``2`` abstained, ``1`` usage/configuration error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .backends import BACKEND_CHOICES, FatalBackendError
from .config import force_utf8_streams, settings
from .escalation import build_work_item, default_title, get_escalation_sink
from .models import Passage, Result
from .pipeline import ABSTAIN, answer_question
from .registry import _norm_trial_id


def _print_provenance(passages: list[Passage]) -> None:
    print("\nProvenance:")
    for p in passages:
        tag = "  [superseded] " if p.superseded_conflict else "  [current]    "
        print(f"{tag}{p.citation()}")


def _print_usage(res: Result) -> None:
    u = res.usage
    if not u.calls:
        return
    note = " (cached)" if u.cached else ""
    print(f"\nTokens{note}: {u.total_tokens} total across {u.calls} call(s) "
          f"— in {u.input_tokens}, out {u.output_tokens}, "
          f"thinking {u.thinking_tokens} · {res.elapsed_ms} ms")


def _confirm_escalation(args) -> bool:
    """Ask whether to escalate. Guards against over-escalation: the default is
    to LEAVE the question unless the user (or a flag) opts in."""
    if args.no_escalate:
        return False
    if args.escalate:
        return True
    if not sys.stdin.isatty():
        print("\n(Non-interactive session; pass --escalate to raise a work item. "
              "Leaving the question un-escalated by default.)")
        return False
    try:
        ans = input("\nEscalate this unanswered question to the designated team? "
                    "[y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _abstain(res: Result, args) -> int:
    print(ABSTAIN)
    print(f"\nReason: {res.reason}")
    if res.unsupported:
        print("\nUnsupported claims:")
        for c in res.unsupported:
            print(f"  - {c}")

    item = build_work_item(default_title(res.trial_id), res.trial_id,
                           res.question, res.reason)
    if _confirm_escalation(args):
        result = get_escalation_sink().send(item)
        if result["mode"] == "file":
            print(f"\n[escalation] ADO MCP not configured — wrote work item to "
                  f"{result['path']}")
        else:
            print("\n[escalation] Created via ADO MCP tool 'wit_create_work_item'.")
        print(json.dumps(result["ado_payload"], indent=2))
    else:
        print("\n[escalation] Not escalated — question left unanswered "
              "(no work item created).")
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vera", description="VERA — version-explicit protocol Q&A")
    ap.add_argument("question", help="the coordinator's question")
    ap.add_argument("--trial", required=True, help="trial id, e.g. 412")
    ap.add_argument("--as-of", default=None,
                    help="as-of date YYYY-MM-DD (default: today)")
    ap.add_argument("--backend", default=settings.backend, choices=BACKEND_CHOICES,
                    help="LLM backend (default: auto -> real provider if a key "
                         "is set, else the deterministic mock)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full structured result as JSON")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the in-process answer cache")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log pipeline diagnostics to stderr")
    esc = ap.add_mutually_exclusive_group()
    esc.add_argument("--escalate", action="store_true",
                     help="on abstention, escalate without prompting")
    esc.add_argument("--no-escalate", action="store_true",
                     help="on abstention, never escalate")
    return ap


def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s] %(name)s: %(message)s", stream=sys.stderr)

    try:
        res = answer_question(args.question, args.trial, args.as_of,
                              args.backend, use_cache=not args.no_cache)
    except FatalBackendError as exc:
        print(f"Backend configuration error: {exc.display()}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
        return 0 if res.answered else 2

    if res.governing is not None:
        print(f"Trial {res.governing.trial_id}: governing version "
              f"v{res.governing.version} (effective {res.governing.effective_date}) "
              f"as of {res.as_of}")
    if res.backend_error:
        print(f"\n[warning] {res.backend_error}", file=sys.stderr)

    if not res.answered:
        rc = _abstain(res, args)
        _print_usage(res)
        return rc

    label = res.backend + (f" · {res.model}" if res.model else "")
    print(f"\n[backend: {label}]")
    print(res.answer)
    _print_provenance(res.passages)
    print(f"\nVerifier: PASSED — {res.verdict.get('reason', '')}")
    _print_usage(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
