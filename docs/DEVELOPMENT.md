# Development guide

Setting up, running, testing, and the conventions to follow. Written for macOS
(the project moved there from Windows in Nov 2026); Linux is identical, and
Windows differences are noted where they matter.

---

## Fresh machine setup (macOS)

```bash
# 1. Python 3.11+ (3.12 matches render.yaml's PYTHON_VERSION 3.12.6 and the Dockerfile)
brew install python@3.12            # skip if `python3 --version` is already >= 3.11

# 2. Clone and create a virtualenv
git clone https://github.com/thebitanpaul/VERA.git
cd VERA
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

# 3. Install (runtime + pytest; includes the anthropic SDK)
pip install -r requirements-dev.txt

# 4. Build the manifest (gitignored build artefact)
python -m vera.ingest --check

# 5. Verify
pytest                               # expect 140 passed
python -m vera "Is concomitant metformin permitted?" --trial 412 --no-escalate
```

### Secrets (`.env`)

`.env` is gitignored, so it does **not** come with the clone. Recreate it:

```bash
cp .env.example .env                 # then add whichever keys you have
```

Nothing is required — with no keys VERA uses the deterministic mock. Typical
local keys: `GEMINI_API_KEY` (free tier). **Leave `GEMINI_MODEL` unset.** Real
environment variables override `.env`.

### What does *not* come across from the old machine

| Item | Status | Action |
|---|---|---|
| `venv/` | gitignored, Windows-specific | recreate (step 2–3) |
| `.env` | gitignored | recreate from `.env.example` |
| `data/manifest.json` | gitignored build artefact | `python -m vera.ingest --check` |
| `var/escalations/*.json` | gitignored demo output | nothing to carry (was empty) |
| Claude Code auto-memory | stored per machine under `~/.claude/projects/…` | not needed — `CLAUDE.md` + `docs/` hold the context |
| Claude Code user settings/plugins | per user, not in repo | reinstall as desired |
| Render deploy | tied to GitHub `main`, not to a machine | nothing to do; pushes still auto-deploy |

---

## Day-to-day commands

| Task | Command |
|---|---|
| Run the web UI | `PORT=5001 python -m web` → http://127.0.0.1:5001 |
| Ask from the CLI | `python -m vera "<question>" --trial 412 [--as-of YYYY-MM-DD] [--backend mock\|gemini\|openai\|xai\|anthropic] [--json] [--no-cache] [-v] [--no-escalate\|--escalate]` |
| Rebuild manifest | `python -m vera.ingest --check` (after any change in `data/protocols/`) |
| Inspect registry | `python -m vera.registry --dump` / `python -m vera.registry 412 2023-07-01` |
| What changed | `python -m vera.query --all` |
| Tests | `pytest` (or `pytest tests/test_pipeline.py -k metformin`) |
| Production-like server | `gunicorn wsgi:app --bind 127.0.0.1:8000 --workers 2 --threads 4` |
| Container | `docker build -t vera . && docker run -p 8000:8000 vera` |
| Lint (optional) | `pip install ruff && ruff check .` — config in `pyproject.toml`; not part of CI |

### macOS gotchas

- **Port 5000 is taken by AirPlay Receiver** (macOS 12+). `python -m web` on the
  default port fails or hits ControlCenter (403). Use `PORT=5001`, or disable
  System Settings → General → AirDrop & Handoff → AirPlay Receiver.
- Use `python3` to create the venv; inside an activated venv `python` works, and
  the Claude Code skill runs `python -m vera` — so **activate the venv** before
  using the skill, or it will hit system Python without Flask/pypdf.
- `vera.config.force_utf8_streams()` exists for the Windows cp1252 console; it is
  harmless on macOS.
- The repo index is LF; no `.gitattributes`. On macOS leave `core.autocrlf` unset.

---

## Testing

- 140 tests across `tests/test_{backends,credentials,ingest,pipeline,registry,retrieval,web}.py`,
  ~8 s, all offline.
- `tests/conftest.py` has an **autouse** `isolated_settings` fixture: removes
  provider keys from the env, sets `VERA_BACKEND=mock`, points
  `VERA_ESCALATION_DIR` at `tmp_path`, reloads settings and rebinds the
  `settings` name in every loaded `vera.*`/`web*` module, and clears the
  answer cache. Other fixtures: `manifest`, `trial_412`, `client` (Flask test client).
- Network calls in backend tests are stubbed; never add a test that needs a key.
- `pytest` config: `-q --strict-markers`; `DeprecationWarning` from `vera.*` is
  an error.
- When fixing a bug, add a regression test (history shows this consistently,
  e.g. the CSS `[hidden]` override guard in `test_web.py`).

---

## Conventions

**Code**
- Python ≥ 3.11, `from __future__ import annotations`, type hints, dataclasses.
- Prefer the stdlib. No YAML, dotenv, requests, or provider SDKs except
  `anthropic` (lazy). New dependencies must be justified; bound them to a major
  in `requirements.txt` and `pyproject.toml`.
- Every tunable goes in `vera/config.py` `Settings` + `from_env()` and is
  documented in `.env.example` (and `render.yaml` if prod needs it).
- Modules bind `from .config import settings` at import time; tests rely on
  the conftest rebinding — keep using that pattern rather than caching values
  in module globals.
- Pipeline semantics live in `vera/`; `web/` only validates and serialises
  (`Result.to_dict()`). The front-end makes no protocol decisions.
- Backends: raise `RetryableBackendError` for 429/5xx/timeout and
  `FatalBackendError` for auth/model/bad-request; record usage; always send an
  output cap.
- Docstrings and comments explain *why*, citing the real failure when there was one.
- Errors shown to users must be safe: no tracebacks, no keys.

**Front-end**
- Escape all interpolated text with `esc()` before `innerHTML`.
- No inline event handlers or inline scripts (CSP blocks them); the one inline
  script in `index.html` carries `nonce="{{ csp_nonce }}"`.
- Reference static assets through `static_url()` in templates (mtime cache-busting).
- `[hidden]` needs an explicit `display: none` rule on any class that sets `display`.

**Protocol data**
- Markdown front-matter: `trial_id`, `title`, `version`, `effective_date`
  (ISO), `status` (`active`|`superseded`, exactly one active per trial),
  `supersedes`, `sections` (declared list).
- Keep section ids stable across versions so change disclosure works.
- After editing, run `python -m vera.ingest --check` and the tests.

**Git**
- Work on `main` (single-developer repo; Render auto-deploys `main` on commit).
- Imperative subject line; body explains cause, evidence, and test count.
- Never commit `.env` or keys. Pre-push check:
  `git ls-files | grep -x ".env"` must print nothing.

---

## Working with Claude Code on this repo

- `CLAUDE.md` (root) is loaded automatically; these docs are the long form.
- The `protocol-qa` skill (`.claude/skills/protocol-qa/SKILL.md`) is for
  *answering protocol questions*, not for developing VERA.
- If you change CLI output format, the abstention string, or flags, update the
  skill and README examples in the same change.
- Keep `docs/STATUS.md` current when finishing a feature or discovering a
  limitation, so the next session (on any machine) starts with the right context.
