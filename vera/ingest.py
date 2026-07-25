"""VERA protocol ingestion pipeline.

Parses clinical-trial protocol documents into a single normalized manifest that
downstream VERA question-answering can query. Two source formats are supported:

  * Markdown protocols with a YAML front-matter block (the synthetic Trial 412
    documents). Front-matter carries version metadata; ## headings become sections.
  * PDF protocols (the real Syndax/entinostat protocol). The cover page and
    amendment metadata are parsed into the same normalized record shape.

Design notes
------------
Both formats emit the SAME record schema so VERA does not need to special-case the
source when answering questions such as "what changed in the latest version?".
Per trial, exactly one version is marked ``active``; the rest are ``superseded``.

Run:
    python -m vera.ingest            # ingest data/protocols -> data/manifest.json
    python -m vera.ingest --check    # ingest and print a human-readable summary
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from .config import settings

# Paths come from config so a deployment can relocate the corpus without a code
# change (VERA_PROTOCOLS_DIR / VERA_MANIFEST_PATH).
ROOT = settings.root
PROTOCOLS_DIR = settings.protocols_dir
MANIFEST_PATH = settings.manifest_path


# ---------------------------------------------------------------------------
# Normalized record schema (shared by both source formats)
# ---------------------------------------------------------------------------
@dataclass
class Section:
    id: str          # e.g. "5.2"
    heading: str     # e.g. "Eligibility - Concomitant Metformin"
    text: str        # section body prose


@dataclass
class ProtocolRecord:
    doc_id: str                       # stable id for the document family, e.g. "412"
    trial_id: str                     # human trial label
    title: str
    version: str
    effective_date: str               # ISO YYYY-MM-DD
    status: str                       # "active" | "superseded"
    supersedes: str                   # prior version string, or ""
    source_file: str                  # basename of the source document
    source_format: str                # "markdown" | "pdf"
    sections: list[Section] = field(default_factory=list)
    amendment_history: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Minimal YAML front-matter parser (no third-party YAML dependency needed for
# the flat key/scalar + simple list structure these protocols use).
# ---------------------------------------------------------------------------
def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError("markdown protocol is missing a YAML front-matter block")
    end = text.find("\n---", 3)
    if end == -1:
        raise ValueError("unterminated YAML front-matter block")
    block = text[3:end].strip("\n")
    body = text[end + len("\n---"):].lstrip("\n")

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if re.match(r"^\s*-\s+", raw) and current_list_key:
            meta[current_list_key].append(_scalar(raw.strip()[1:].strip()))
            continue
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            meta[key] = []          # start of a block list
            current_list_key = key
        else:
            meta[key] = _scalar(val)
            current_list_key = None
    return meta, body


def _scalar(val: str) -> str:
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    return val


# ---------------------------------------------------------------------------
# Markdown protocol parsing
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
# Matches "5.2 Eligibility - Concomitant Metformin" / "6. Washout Period, Cohort B"
_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")


def parse_markdown_protocol(path: Path) -> ProtocolRecord:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    sections: list[Section] = []
    current: Section | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is not None:
            current.text = "\n".join(buf).strip()
            sections.append(current)

    for line in body.splitlines():
        hm = _HEADING_RE.match(line)
        if hm:
            heading_text = hm.group(1).strip().replace("—", "-")
            sm = _SECTION_RE.match(heading_text)
            if sm:                       # numbered section like "5.2 ..." -> start new section
                flush()
                buf = []
                current = Section(id=sm.group(1), heading=sm.group(2).strip(), text="")
                continue
        if current is not None:
            buf.append(line)
    flush()

    return ProtocolRecord(
        doc_id=str(meta.get("trial_id", path.stem)),
        trial_id=f"Trial {meta.get('trial_id')}" if meta.get("trial_id") else path.stem,
        title=str(meta.get("title", "")),
        version=str(meta.get("version", "")),
        effective_date=str(meta.get("effective_date", "")),
        status=str(meta.get("status", "")),
        supersedes=str(meta.get("supersedes", "")),
        source_file=path.name,
        source_format="markdown",
        sections=sections,
        amendment_history={
            "supersedes_version": str(meta.get("supersedes", "")) or None,
        },
        extra={"declared_sections": meta.get("sections", [])},
    )


# ---------------------------------------------------------------------------
# PDF protocol parsing (cover page + amendment metadata)
# ---------------------------------------------------------------------------
_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _iso_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    day, mon, year = m.group(1), m.group(2).lower(), m.group(3)
    if mon not in _MONTHS:
        return None
    return f"{year}-{_MONTHS[mon]}-{int(day):02d}"


def parse_pdf_protocol(path: Path) -> ProtocolRecord:
    reader = PdfReader(str(path))
    cover = reader.pages[0].extract_text() or ""

    def grab(pattern: str) -> str | None:
        m = re.search(pattern, cover, re.I)
        return m.group(1).strip() if m else None

    product = grab(r"Product:\s*(.+)")
    protocol_number = grab(r"Protocol Number:\s*(\S+)")
    version = grab(r"Version\s*([0-9]+\.[0-9]+)")
    amendment = grab(r"Amendment:\s*(\d+)")
    nct = grab(r"(NCT\d{8})")
    ind = grab(r"IND Number:\s*([\d,]+)")
    eudract = grab(r"EudraCT Number:\s*([\d-]+)")
    sponsor = grab(r"Clinical Study Sponsor:\s*(.+)")
    date_iso = _iso_date(cover)

    # Reconstruct the study title from the cover page (spans several lines).
    title = ""
    tm = re.search(r"(A Phase [^\n]+(?:\n[^\n]+){0,3}?Colorectal Cancer)", cover)
    if tm:
        title = re.sub(r"\s+", " ", tm.group(1)).strip()

    # Locate the amendment procedure section (15.1) for provenance. Note: this
    # real protocol carries only cumulative cover-page metadata (Version 6.0,
    # Amendment 5) and a procedural amendment clause -- it does NOT contain a
    # per-amendment "summary of changes" change log.
    amendment_clause = None
    for page in reader.pages:
        t = page.extract_text() or ""
        if "Protocol Amendments and Study Termination" in t:
            idx = t.find("Protocol Amendments and Study Termination")
            amendment_clause = re.sub(r"\s+", " ", t[idx:idx + 600]).strip()
            break

    amendment_history = {
        "current_version": version,
        "amendment_number": int(amendment) if amendment else None,
        "effective_date": date_iso,
        "has_change_log": False,
        "change_log": [],
        "note": (
            "Source PDF is Version {v} (Amendment {a}). The document does not "
            "contain a per-amendment summary-of-changes / change-log section; only "
            "cumulative cover-page metadata and the procedural amendment clause "
            "(Section 15.1) are present. VERA should abstain from itemizing "
            "specific changes for this document and instead report the current "
            "version/amendment level.".format(v=version, a=amendment)
        ),
        "amendment_clause_excerpt": amendment_clause,
    }

    return ProtocolRecord(
        doc_id=protocol_number or path.stem,
        trial_id=protocol_number or path.stem,
        title=title,
        version=version or "",
        effective_date=date_iso or "",
        status="active",            # ingested as the single active version
        supersedes="",
        source_file=path.name,
        source_format="pdf",
        sections=[],                # full-text section extraction is out of scope here
        amendment_history=amendment_history,
        extra={
            "product": product,
            "protocol_number": protocol_number,
            "amendment_number": int(amendment) if amendment else None,
            "nct": nct,
            "ind_number": ind,
            "eudract_number": eudract,
            "sponsor": sponsor,
            "page_count": len(reader.pages),
        },
    )


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------
def _record_to_dict(rec: ProtocolRecord) -> dict[str, Any]:
    d = asdict(rec)
    d["sections"] = [asdict(s) for s in rec.sections]
    return d


def _relpath(p: Path) -> str:
    """Repo-relative path when possible, absolute otherwise (relocated corpora)."""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def build_manifest(protocols_dir: Path = PROTOCOLS_DIR) -> dict[str, Any]:
    records: list[ProtocolRecord] = []

    for md in sorted(protocols_dir.glob("*.md")):
        records.append(parse_markdown_protocol(md))
    for pdf in sorted(protocols_dir.glob("*.pdf")):
        records.append(parse_pdf_protocol(pdf))

    # Group by doc_id and validate the single-active-version invariant.
    families: dict[str, list[ProtocolRecord]] = {}
    for rec in records:
        families.setdefault(rec.doc_id, []).append(rec)

    warnings: list[str] = []
    trials = []
    for doc_id, versions in sorted(families.items()):
        versions.sort(key=lambda r: r.version or "")
        active = [v for v in versions if v.status == "active"]
        if len(active) != 1:
            warnings.append(
                f"{doc_id}: expected exactly 1 active version, found {len(active)}"
            )
        trials.append({
            "doc_id": doc_id,
            "active_version": active[0].version if active else None,
            "versions": [_record_to_dict(v) for v in versions],
        })

    return {
        "generated_by": "vera.ingest",
        "protocols_dir": _relpath(protocols_dir),
        "trial_count": len(trials),
        "document_count": len(records),
        "warnings": warnings,
        "trials": trials,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="VERA protocol ingestion pipeline")
    ap.add_argument("--check", action="store_true", help="print a human-readable summary")
    args = ap.parse_args()

    manifest = build_manifest()
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote {_relpath(MANIFEST_PATH)} "
          f"({manifest['document_count']} documents, {manifest['trial_count']} trials)")
    for w in manifest["warnings"]:
        print(f"  WARNING: {w}")

    if args.check:
        for trial in manifest["trials"]:
            print(f"\n{trial['doc_id']}  (active version: {trial['active_version']})")
            for v in trial["versions"]:
                print(f"  - v{v['version']:<4} {v['status']:<10} "
                      f"{v['effective_date']}  [{v['source_format']}]  {v['source_file']}")


if __name__ == "__main__":
    main()
