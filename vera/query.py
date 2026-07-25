"""Small query helpers over the ingested manifest.

Demonstrates that the ingestion output supports the target question
"what changed in the latest version?" for each trial:

  * For markdown trials with section text (Trial 412), it diffs the active
    version's sections against the version it supersedes and reports which
    sections changed.
  * For the real PDF protocol, which carries no per-amendment change log, it
    abstains from itemizing changes and reports the version/amendment level
    instead -- the honest answer given the source document.

Run:
    python -m vera.query "412"
    python -m vera.query "SNDX-275-0601"
    python -m vera.query --all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import settings

ROOT = settings.root
MANIFEST_PATH = settings.manifest_path


def _load() -> dict:
    if not MANIFEST_PATH.exists():
        sys.exit(f"{MANIFEST_PATH} not found -- run `python -m vera.ingest` first")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    return " ".join(text.split())


def what_changed(doc_id: str, manifest: dict) -> str:
    trial = next((t for t in manifest["trials"] if t["doc_id"] == doc_id), None)
    if trial is None:
        return f"No ingested trial with doc_id '{doc_id}'."

    versions = {v["version"]: v for v in trial["versions"]}
    active = next((v for v in trial["versions"] if v["status"] == "active"), None)
    if active is None:
        return f"{doc_id}: no active version found."

    # PDF-style documents with no change log -> abstain from itemizing changes.
    if not active.get("sections"):
        ah = active.get("amendment_history", {})
        if not ah.get("has_change_log", False):
            return (
                f"{doc_id}: the active document is Version {active['version']} "
                f"(Amendment {ah.get('amendment_number')}), effective "
                f"{active['effective_date']}. The source document does not enumerate "
                f"what changed between amendments, so a specific change list cannot be "
                f"provided from it."
            )

    prior_ver = active.get("supersedes") or ""
    prior = versions.get(prior_ver)
    if not prior:
        return (
            f"{doc_id}: active Version {active['version']} is the earliest ingested "
            f"version; there is no prior version to diff against."
        )

    prior_secs = {s["id"]: s for s in prior["sections"]}
    changes = []
    for s in active["sections"]:
        p = prior_secs.get(s["id"])
        if p is None:
            changes.append(f"§{s['id']} {s['heading']}: added")
        elif _norm(p["text"]) != _norm(s["text"]):
            changes.append(f"§{s['id']} {s['heading']}: modified")
    active_ids = {s["id"] for s in active["sections"]}
    for pid, p in prior_secs.items():
        if pid not in active_ids:
            changes.append(f"§{pid} {p['heading']}: removed")

    header = (
        f"{doc_id}: changes in Version {active['version']} "
        f"(effective {active['effective_date']}) vs Version {prior_ver}:"
    )
    if not changes:
        return header + "\n  (no section-level differences detected)"
    return header + "\n" + "\n".join(f"  - {c}" for c in sorted(changes))


def main() -> None:
    ap = argparse.ArgumentParser(description="Query the VERA manifest")
    ap.add_argument("doc_id", nargs="?", help="trial doc_id, e.g. 412 or SNDX-275-0601")
    ap.add_argument("--all", action="store_true", help="report for every trial")
    args = ap.parse_args()

    manifest = _load()
    if args.all or not args.doc_id:
        for t in manifest["trials"]:
            print(what_changed(t["doc_id"], manifest))
            print()
    else:
        print(what_changed(args.doc_id, manifest))


if __name__ == "__main__":
    main()
