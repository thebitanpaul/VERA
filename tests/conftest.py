"""Shared fixtures.

Every test runs against the deterministic mock backend and an isolated
escalation directory, so the suite never touches the network, never needs a
key, and never writes into the repo.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Force the mock backend and redirect writable paths at a temp dir."""
    from vera import config, pipeline

    for var in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY",
                "GEMINI_MODEL", "VERA_ADO_MCP", "VERA_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VERA_ESCALATION_DIR", str(tmp_path / "escalations"))
    monkeypatch.setenv("VERA_BACKEND", "mock")

    config.reload_settings()
    # Modules bind `settings` with `from .config import settings`, so the name is
    # captured at import time. Rebind it everywhere rather than maintaining a
    # hand-written list that silently goes stale when a module is added.
    for name, module in list(sys.modules.items()):
        if not (name == "vera" or name.startswith(("vera.", "web"))):
            continue
        if getattr(module, "settings", None) is not None:
            monkeypatch.setattr(module, "settings", config.settings, raising=False)

    pipeline.clear_cache()
    yield config.settings
    pipeline.clear_cache()
    config.reload_settings()


@pytest.fixture
def manifest():
    from vera.pipeline import load_manifest
    return load_manifest()


@pytest.fixture
def trial_412(manifest):
    """(current_record, older_records) for Trial 412's governing version."""
    from vera.pipeline import trial_versions
    from vera.registry import build_registry

    governing = build_registry().resolve_current_version("412", "2026-01-01")
    versions = trial_versions(manifest, "412")
    current = versions[governing.version]
    older = [v for k, v in versions.items() if k != governing.version]
    return current, older


@pytest.fixture
def client():
    from web import create_app
    app = create_app({"TESTING": True})
    with app.test_client() as c:
        yield c
