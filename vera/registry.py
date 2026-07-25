"""VERA Protocol Registry -- Architecture component [2].

The registry is the *deterministic* data product that answers the single
question "which protocol version governs trial X as of date D?". Per the system
architecture, this decision is made here by explicit rule -- **never** by the
LLM. The query resolver ([3]) and grounded synthesis ([5]) layers must call
:func:`resolve_current_version` rather than reasoning about versions themselves.

The registry is built purely from the YAML front-matter of every markdown file
in the protocols directory (documents without front-matter, e.g. raw PDFs, are
skipped -- they carry no declared trial_id/version/status/effective_date to
govern by).

Governing rule
--------------
    resolve_current_version(trial_id, as_of_date) returns the version with the
    latest effective_date on or before as_of_date. If no version had taken
    effect by that date, returns None.

That is: a version governs from its own effective_date until the next version's
effective_date. Asked as of today this yields the currently ``active`` document;
asked as of a past date it yields the version that was genuinely in force then,
which is what a coordinator reconstructing a historical decision needs.

The ``status`` field records which single version is active *today* and is
validated at ingestion (exactly one per trial). It is deliberately NOT used to
filter resolution: a version being superseded now says nothing about whether it
governed on the date being asked about. Callers can read
:attr:`RegistryEntry.status` to tell a historical answer from a current one --
:func:`ProtocolRegistry.is_historical` is the convenience for that.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

# Reuse the ingestion front-matter parser and paths so the registry and the
# ingestion pipeline never diverge on how metadata is read.
from .config import settings
from .ingest import _parse_frontmatter

PROTOCOLS_DIR = settings.protocols_dir

ACTIVE = "active"


@dataclass(frozen=True)
class RegistryEntry:
    trial_id: str
    version: str
    effective_date: _dt.date
    status: str
    supersedes: str
    source_file: str


def _norm_trial_id(trial_id: str) -> str:
    """Accept '412', 'Trial 412', ' 412 ' -> '412'."""
    tid = str(trial_id).strip()
    if tid.lower().startswith("trial "):
        tid = tid[len("trial "):].strip()
    return tid


def _as_date(value: str | _dt.date) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value).strip())


class ProtocolRegistry:
    """In-memory registry indexed by trial_id."""

    def __init__(self, entries: list[RegistryEntry]):
        self._by_trial: dict[str, list[RegistryEntry]] = {}
        for e in entries:
            self._by_trial.setdefault(e.trial_id, []).append(e)
        # Keep each trial's versions sorted by effective_date (stable, ascending).
        for lst in self._by_trial.values():
            lst.sort(key=lambda e: e.effective_date)

    @property
    def trial_ids(self) -> list[str]:
        return sorted(self._by_trial)

    def versions(self, trial_id: str) -> list[RegistryEntry]:
        return list(self._by_trial.get(_norm_trial_id(trial_id), []))

    def first_effective_date(self, trial_id: str) -> _dt.date | None:
        """Earliest date on which any version of this trial took effect.

        Below this, no protocol existed to govern -- the UI uses it as the lower
        bound of the date picker so the state is unreachable by mistake.
        """
        entries = self._by_trial.get(_norm_trial_id(trial_id), [])
        return entries[0].effective_date if entries else None

    def resolve_current_version(
        self, trial_id: str, as_of_date: str | _dt.date
    ) -> RegistryEntry | None:
        """Authoritative resolution of the governing version.

        Returns the version with the latest effective_date on or before
        ``as_of_date`` -- i.e. the one in force on that date, since a version
        governs until the next one takes effect. Deterministic; the LLM must
        never make this decision.

        Returns None only when the date precedes every version of the trial (or
        the trial is unknown). Read :attr:`RegistryEntry.status` on the result to
        tell whether the answer reflects the current protocol or a superseded one.
        """
        as_of = _as_date(as_of_date)
        candidates = [
            e
            for e in self._by_trial.get(_norm_trial_id(trial_id), [])
            if e.effective_date <= as_of
        ]
        if not candidates:
            return None
        # Latest effective_date wins; tie-break deterministically on version.
        return max(candidates, key=lambda e: (e.effective_date, e.version))

    def is_historical(self, entry: RegistryEntry | None) -> bool:
        """True when ``entry`` governed on the date asked about but has since
        been superseded -- the signal the UI turns into a 'historical view' banner."""
        return entry is not None and entry.status != ACTIVE


def build_registry(protocols_dir: Path = PROTOCOLS_DIR) -> ProtocolRegistry:
    """Build the registry from the front-matter of every markdown protocol."""
    entries: list[RegistryEntry] = []
    for md in sorted(Path(protocols_dir).glob("*.md")):
        meta, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
        # A file without the governing keys cannot participate in resolution.
        if not meta.get("trial_id") or not meta.get("version"):
            continue
        entries.append(
            RegistryEntry(
                trial_id=_norm_trial_id(str(meta["trial_id"])),
                version=str(meta["version"]),
                effective_date=_as_date(str(meta["effective_date"])),
                status=str(meta.get("status", "")).strip().lower(),
                supersedes=str(meta.get("supersedes", "")),
                source_file=md.name,
            )
        )
    return ProtocolRegistry(entries)


def resolve_current_version(
    trial_id: str,
    as_of_date: str | _dt.date,
    protocols_dir: Path = PROTOCOLS_DIR,
) -> RegistryEntry | None:
    """Convenience: build the registry and resolve in one call."""
    return build_registry(protocols_dir).resolve_current_version(trial_id, as_of_date)


def main() -> None:
    ap = argparse.ArgumentParser(description="VERA protocol registry")
    ap.add_argument("trial_id", nargs="?", help="e.g. 412")
    ap.add_argument("as_of_date", nargs="?", help="YYYY-MM-DD")
    ap.add_argument("--dump", action="store_true", help="print the full registry table")
    args = ap.parse_args()

    reg = build_registry()

    if args.dump or not args.trial_id:
        for tid in reg.trial_ids:
            print(f"Trial {tid}:")
            for e in reg.versions(tid):
                print(f"  v{e.version:<4} {e.status:<11} eff {e.effective_date} "
                      f"supersedes={e.supersedes!r}  [{e.source_file}]")
        if not args.trial_id:
            return

    if args.trial_id and args.as_of_date:
        gov = reg.resolve_current_version(args.trial_id, args.as_of_date)
        if gov is None:
            print(f"\nGoverning version for Trial {_norm_trial_id(args.trial_id)} "
                  f"as of {args.as_of_date}: NONE (no active version effective yet)")
        else:
            print(f"\nGoverning version for Trial {gov.trial_id} as of {args.as_of_date}: "
                  f"v{gov.version} (effective {gov.effective_date}, {gov.source_file})")


if __name__ == "__main__":
    main()
