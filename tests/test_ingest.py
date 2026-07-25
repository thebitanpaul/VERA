"""Ingestion produces one normalized record shape from two source formats."""

from __future__ import annotations

from vera.config import settings
from vera.ingest import _parse_frontmatter, build_manifest, parse_markdown_protocol


def test_frontmatter_parses_scalars_and_lists():
    meta, body = _parse_frontmatter(
        '---\ntrial_id: "412"\nversion: 3.0\nsections:\n  - 5.2\n  - 6\n---\n\n## 1 Intro\ntext\n')
    assert meta["trial_id"] == "412"
    assert meta["version"] == "3.0"
    assert meta["sections"] == ["5.2", "6"]
    assert body.startswith("## 1 Intro")


def test_markdown_protocol_sections():
    rec = parse_markdown_protocol(settings.protocols_dir / "412_v3.0.md")
    assert rec.doc_id == "412"
    assert rec.version == "3.0"
    assert rec.status == "active"
    assert rec.supersedes == "2.0"
    assert rec.source_format == "markdown"
    ids = [s.id for s in rec.sections]
    assert "5.2" in ids and "6" in ids
    assert all(s.text for s in rec.sections)


def test_manifest_has_one_active_version_per_trial():
    manifest = build_manifest()
    assert manifest["warnings"] == []
    for trial in manifest["trials"]:
        active = [v for v in trial["versions"] if v["status"] == "active"]
        assert len(active) == 1, trial["doc_id"]
        assert trial["active_version"] == active[0]["version"]


def test_pdf_protocol_is_ingested_and_declines_to_invent_a_change_log():
    manifest = build_manifest()
    record = next(v for t in manifest["trials"] for v in t["versions"]
                  if v["source_format"] == "pdf")
    assert record["version"]
    assert record["amendment_history"]["has_change_log"] is False
    assert record["amendment_history"]["change_log"] == []


def test_both_formats_share_the_record_schema():
    manifest = build_manifest()
    keys = {frozenset(v) for t in manifest["trials"] for v in t["versions"]}
    assert len(keys) == 1, "markdown and pdf records diverged"
