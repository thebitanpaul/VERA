"""The registry is the safety-critical component: it decides, deterministically,
which protocol version governs. These tests pin that rule."""

from __future__ import annotations

import datetime as dt

import pytest

from vera.registry import RegistryEntry, ProtocolRegistry, _norm_trial_id, build_registry


@pytest.mark.parametrize("raw,expected", [
    ("412", "412"), (" 412 ", "412"), ("Trial 412", "412"),
    ("trial 412", "412"), ("TRIAL  412", "412"), ("  Trial  412  ", "412"),
])
def test_norm_trial_id(raw, expected):
    assert _norm_trial_id(raw) == expected


def test_registry_loads_all_markdown_versions():
    reg = build_registry()
    assert reg.trial_ids == ["412", "778"]   # the PDF has no front matter
    assert [e.version for e in reg.versions("412")] == ["1.0", "2.0", "3.0"]
    assert [e.version for e in reg.versions("778")] == ["1.0", "2.0"]


@pytest.mark.parametrize("as_of,expected", [
    # A version governs from its own effective_date until the next takes effect,
    # so a past date resolves to the version genuinely in force then.
    ("2023-05-31", None),      # before ANY version existed -> nothing governs
    ("2023-06-01", "1.0"),     # boundary: effective_date is inclusive
    ("2024-01-31", "1.0"),     # v1.0 runs right up to v2.0
    ("2024-02-01", "2.0"),
    ("2025-02-28", "2.0"),
    ("2025-03-01", "3.0"),
    ("2030-01-01", "3.0"),     # the latest version persists
])
def test_resolve_current_version_by_date(as_of, expected):
    got = build_registry().resolve_current_version("412", as_of)
    assert (got.version if got else None) == expected


def test_effective_date_boundary_is_inclusive():
    reg = ProtocolRegistry([
        RegistryEntry("999", "1.0", dt.date(2024, 5, 10), "active", "", "a.md"),
    ])
    assert reg.resolve_current_version("999", "2024-05-09") is None
    assert reg.resolve_current_version("999", "2024-05-10").version == "1.0"


def test_a_version_not_yet_effective_does_not_govern():
    reg = ProtocolRegistry([
        RegistryEntry("999", "1.0", dt.date(2020, 1, 1), "superseded", "", "a.md"),
        RegistryEntry("999", "2.0", dt.date(2022, 1, 1), "active", "1.0", "b.md"),
        RegistryEntry("999", "3.0", dt.date(2030, 1, 1), "active", "2.0", "c.md"),
    ])
    assert reg.resolve_current_version("999", "2025-01-01").version == "2.0"


def test_superseded_version_still_governs_its_own_era():
    """`status` records what is current TODAY; it must not rewrite history."""
    reg = build_registry()
    got = reg.resolve_current_version("412", "2023-07-01")
    assert got.version == "1.0"
    assert got.status == "superseded"
    assert reg.is_historical(got) is True


def test_today_resolves_to_the_active_version():
    reg = build_registry()
    got = reg.resolve_current_version("412", "2026-07-25")
    assert got.status == "active"
    assert reg.is_historical(got) is False


def test_first_effective_date():
    reg = build_registry()
    assert reg.first_effective_date("412") == dt.date(2023, 6, 1)
    assert reg.first_effective_date("778") == dt.date(2024, 9, 15)
    assert reg.first_effective_date("nope") is None


def test_resolve_unknown_trial_returns_none():
    assert build_registry().resolve_current_version("000", "2026-01-01") is None


def test_resolve_accepts_date_objects_and_strings():
    reg = build_registry()
    assert (reg.resolve_current_version("412", dt.date(2026, 1, 1)).version
            == reg.resolve_current_version("412", "2026-01-01").version)


def test_versions_are_sorted_by_effective_date():
    dates = [e.effective_date for e in build_registry().versions("412")]
    assert dates == sorted(dates)
