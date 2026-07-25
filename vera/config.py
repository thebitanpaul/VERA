"""Central configuration for VERA.

Every tunable lives here and is overridable from the environment (or ``.env``),
so nothing about deployment requires a code change. Import :data:`settings` --
it is built once at import time.

Cost safety
-----------
The LLM knobs in :class:`Settings` are the guardrails against runaway spend.
They exist because of a real failure mode (see ``docs/ARCHITECTURE.md``):
modern Gemini models reason before answering, thinking tokens are billed and
count against ``maxOutputTokens``, and an uncapped request can therefore burn a
large budget and still return an empty candidate. Every backend must send an
explicit output cap and an explicit thinking level.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# <root>/vera/config.py -> <root>
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# .env loading (no third-party dependency)
# ---------------------------------------------------------------------------
def load_dotenv(path: str | Path | None = None) -> None:
    """Populate ``os.environ`` from a simple ``KEY=value`` file.

    Real environment variables always win, so a platform-provided secret (Render,
    Vercel, CI) is never shadowed by a stale local ``.env``. Values may be quoted
    and may contain ``=``; ``export KEY=value`` is tolerated.
    """
    p = Path(path) if path is not None else ROOT / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key.strip(), val)


def force_utf8_streams() -> None:
    """Re-encode stdout/stderr as UTF-8 where supported.

    VERA's output contains '§', '—' and '→', and the abstention string is
    matched verbatim by the Claude Code skill. A legacy Windows console
    codepage (cp1252) raises UnicodeEncodeError on all of them, which is enough
    to kill a server at startup. Safe to call more than once.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # detached, or not a real stream
                pass


def _env_str(name: str, default: str) -> str:
    val = os.environ.get(name, "").strip()
    return val or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_path(name: str, default: Path) -> Path:
    val = os.environ.get(name, "").strip()
    return Path(val).expanduser() if val else default


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Construct via :meth:`from_env`."""

    # --- paths -------------------------------------------------------------
    root: Path = ROOT
    protocols_dir: Path = ROOT / "data" / "protocols"
    manifest_path: Path = ROOT / "data" / "manifest.json"
    escalation_dir: Path = ROOT / "var" / "escalations"

    # --- retrieval tuning --------------------------------------------------
    # Deliberately conservative: when in doubt, abstain.
    heading_boost: float = 3.0      # a query term in a heading counts triple
    top_k: int = 3                  # max passages handed to synthesis
    low_confidence: float = 0.34    # coverage below this -> abstain
    relative_floor: float = 0.2     # drop passages under this fraction of top hit

    # --- LLM backend -------------------------------------------------------
    backend: str = "auto"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    xai_api_key: str = ""
    xai_model: str = "grok-3-mini"
    voyage_api_key: str = ""
    # Let a browser session supply its own provider + key per request, so a
    # deployment can switch models without a redeploy. Disable to lock a
    # deployment to its server-side credentials.
    allow_client_credentials: bool = True

    # --- cost guardrails ---------------------------------------------------
    # maxOutputTokens on Gemini 3 includes thinking tokens, so these must leave
    # headroom for reasoning or the visible answer comes back empty.
    synth_max_output_tokens: int = 2048
    verify_max_output_tokens: int = 1024
    thinking_level: str = "low"     # "low" | "high"; low is ample for extraction
    max_passage_chars: int = 4000   # truncate any single passage before prompting
    request_timeout: int = 60       # seconds
    max_retries: int = 2            # retryable (429/5xx) failures only
    retry_base_delay: float = 1.5   # seconds; doubled per attempt
    cache_size: int = 256           # answers memoised per process (0 disables)

    # --- escalation --------------------------------------------------------
    ado_mcp_enabled: bool = False
    ado_project: str = "<ADO_PROJECT>"

    # --- web ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = False

    extras: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        root = _env_path("VERA_ROOT", ROOT)
        return cls(
            root=root,
            protocols_dir=_env_path("VERA_PROTOCOLS_DIR", root / "data" / "protocols"),
            manifest_path=_env_path("VERA_MANIFEST_PATH", root / "data" / "manifest.json"),
            escalation_dir=_env_path("VERA_ESCALATION_DIR", root / "var" / "escalations"),

            heading_boost=_env_float("VERA_HEADING_BOOST", 3.0),
            top_k=_env_int("VERA_TOP_K", 3),
            low_confidence=_env_float("VERA_LOW_CONFIDENCE", 0.34),
            relative_floor=_env_float("VERA_RELATIVE_FLOOR", 0.2),

            backend=_env_str("VERA_BACKEND", "auto"),
            gemini_api_key=_env_str("GEMINI_API_KEY", ""),
            gemini_model=_env_str("GEMINI_MODEL", "gemini-flash-latest"),
            anthropic_api_key=_env_str("ANTHROPIC_API_KEY", ""),
            anthropic_model=_env_str("ANTHROPIC_MODEL", "claude-opus-4-5"),
            openai_api_key=_env_str("OPENAI_API_KEY", ""),
            openai_model=_env_str("OPENAI_MODEL", "gpt-4o-mini"),
            xai_api_key=_env_str("XAI_API_KEY", ""),
            xai_model=_env_str("XAI_MODEL", "grok-3-mini"),
            voyage_api_key=_env_str("VOYAGE_API_KEY", ""),
            allow_client_credentials=_env_bool("VERA_ALLOW_CLIENT_KEYS", True),

            synth_max_output_tokens=_env_int("VERA_SYNTH_MAX_TOKENS", 2048),
            verify_max_output_tokens=_env_int("VERA_VERIFY_MAX_TOKENS", 1024),
            thinking_level=_env_str("VERA_THINKING_LEVEL", "low"),
            max_passage_chars=_env_int("VERA_MAX_PASSAGE_CHARS", 4000),
            request_timeout=_env_int("VERA_REQUEST_TIMEOUT", 60),
            max_retries=_env_int("VERA_MAX_RETRIES", 2),
            retry_base_delay=_env_float("VERA_RETRY_BASE_DELAY", 1.5),
            cache_size=_env_int("VERA_CACHE_SIZE", 256),

            ado_mcp_enabled=_env_bool("VERA_ADO_MCP", False),
            ado_project=_env_str("ADO_PROJECT", "<ADO_PROJECT>"),

            host=_env_str("HOST", "127.0.0.1"),
            port=_env_int("PORT", 5000),
            debug=_env_bool("VERA_DEBUG", False),
        )


load_dotenv()
settings = Settings.from_env()


def reload_settings() -> Settings:
    """Rebuild :data:`settings` from the current environment (used by tests)."""
    global settings
    settings = Settings.from_env()
    return settings
