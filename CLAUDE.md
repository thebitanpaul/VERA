# CLAUDE.md — VERA

VERA (Version-Explicit Retrieval & Answering) answers a clinical-trial
coordinator's question about what a protocol **currently** requires, using only
the protocol version that governs the trial on a given date, always cited
(trial, version, section, effective date), and **abstains rather than guessing**.
Live at https://vera.phiuture.com (Render). Proprietary (phiUture Tech) — see LICENSE.

Detailed context lives in `docs/` — read the relevant file before non-trivial work:

| File | Read it when |
|---|---|
| `docs/PROJECT_CONTEXT.md` | you need the goals, philosophy, requirements, or *why* a decision was made |
| `docs/ARCHITECTURE.md` | touching the pipeline, retrieval, backends, escalation, or cost controls |
| `docs/DEVELOPMENT.md` | setting up a machine (macOS notes), running things, conventions, gotchas |
| `docs/STATUS.md` | picking up work: completed features, known limitations, open issues, next steps |
| `docs/DEPLOYMENT.md` | deploying to Render / Docker, runtime provider selection, prod settings |

## Commands

```bash
python3 -m venv venv && source venv/bin/activate   # macOS/Linux (Python >= 3.11)
pip install -r requirements-dev.txt
python -m vera.ingest --check        # build data/manifest.json (gitignored; rebuild after editing data/protocols)
pytest                               # 140 tests, offline, no API key, mock backend forced
python -m vera "Is concomitant metformin permitted?" --trial 412 --no-escalate
PORT=5001 python -m web              # dev UI; on macOS port 5000 is taken by AirPlay Receiver
python -m vera.registry --dump       # print the version registry
python -m vera.query --all           # "what changed in the latest version?" per trial
```

CLI exit codes: `0` answered, `2` abstained, `1` error.

## Non-negotiable invariants

These are the product. Do not weaken them without the user's explicit say-so.

1. **The LLM never decides which version governs.** `vera/registry.py`
   resolves it deterministically: a version governs from its `effective_date`
   until the next version's. `status` is *not* used to filter resolution.
2. **Abstention gates are deterministic** (in `vera/pipeline.py` /
   `vera/retrieval.py`): date before first version, unknown trial, topic absent
   from the governing version, coverage < `VERA_LOW_CONFIDENCE` (0.34), or the
   verifier rejects the answer. Only prose synthesis and the claim audit use a model.
3. **The abstention string is exactly** `INSUFFICIENT EVIDENCE — escalating`
   (em dash). The Claude Code skill matches it verbatim.
4. **Retrieval never crosses trials** and is filtered to the governing version;
   superseded sections are surfaced only for change disclosure.
5. **Never escalate without asking.** Abstain → ask → only then file a work item.
6. **Every LLM call sends an explicit output cap and thinking level.** Only
   `RetryableBackendError` is retried (bounded); `FatalBackendError` never is.
7. **Degrade loudly:** if a live backend fails, the mock answers, `backend_error`
   is set and shown, and the degraded result is **not cached**.
8. **Browser-supplied API keys** are never logged, persisted, or echoed; they
   enter the cache key only as a 12-char SHA-256 prefix. Keep the per-request
   CSP nonce (`web/security.py`) and escape everything via `esc()` in `app.js`.
9. **Do not pin retired Gemini models** (`gemini-2.0/2.5-flash`, `1.5-*`) — they
   return `429 … limit: 0`. Default `gemini-flash-latest` is a rolling alias.

## Conventions

- Python ≥ 3.11, `from __future__ import annotations`, dataclasses, type hints,
  stdlib first (Gemini/OpenAI/xAI use `urllib`; no YAML or dotenv dependency).
  Ruff config in `pyproject.toml` (line length 92) — ruff is not installed by
  `requirements-dev.txt`.
- **All tunables live in `vera/config.py`** and are env-overridable; deployments
  never need a code change. Add new settings there and to `.env.example`.
- **One pipeline, two front-ends**: `vera.pipeline.answer_question` returns a
  `Result`; the CLI and Flask routes only present it. Never re-implement
  pipeline logic in `web/`.
- Comments and docstrings explain *why* (often a real failure that happened).
  Commit messages: imperative subject, body explains the cause and the evidence.
- Tests must stay offline and keyless (`tests/conftest.py` forces the mock and
  a temp escalation dir). Add a regression test with every bug fix.
- Protocol markdown needs YAML front-matter (`trial_id`, `version`,
  `effective_date`, `status`, `supersedes`) with exactly one `active` per trial;
  numbered headings (`## 5.2 Heading` or `## 6. Heading`) become sections, and a
  section keeps the **same id across versions** so changes can be disclosed.
- Secrets only in `.env` (gitignored; template `.env.example`). Never commit keys.

## Claude Code skill

`.claude/skills/protocol-qa/SKILL.md` lets a coordinator ask protocol questions in
Claude Code: it runs `python -m vera ... --no-escalate`, relays the answer with
citations, and asks before re-running with `--escalate`. Never answer protocol
questions from general knowledge — route them through VERA.
