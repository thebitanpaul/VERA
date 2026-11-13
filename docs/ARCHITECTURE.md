# Architecture

How VERA works, module by module, and the engineering behind its cost and
failure handling. Diagram: [architecture.svg](architecture.svg). Rationale for
each decision: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md#architectural-decisions-adr-summary).

> This file was removed from the repo in commit `f691010` and restored (updated
> to match the code) during the Windows → macOS migration. `README.md`,
> `render.yaml`, and `vera/config.py` link to it.

---

## Pipeline

```
Protocol docs ─▶ [1] INGESTION ─▶ [2] REGISTRY ─▶ [3] RESOLVE + RETRIEVE
(md + pdf)         one manifest     which version     governing version only,
                   schema           governs, as-of    + superseded same-id sections
                                                                │
                                         deterministic gates ◀──┘
                                         (abstain early, no LLM call)
                                                                │ pass
                                                                ▼
                         [4] SYNTHESIS (LLM) ─▶ [5] VERIFIER (LLM audit)
                         answer only from        grounded? citations present?
                         passages, cite them             │
                                         ┌──────── pass ─┴─ fail ────────┐
                                         ▼                               ▼
                               [6a] ANSWER + provenance        [6b] ABSTAIN
                                                                      │ user confirms
                                                                      ▼
                                                               [7] ESCALATE
```

Note: the docstring in `vera/pipeline.py` numbers its own steps [1]–[4]
(registry, retrieval, synthesis, verifier); module docstrings elsewhere use the
original 8-step numbering below. Same pipeline, two numberings.

## Module map

| Step | Module | Deterministic? |
|---|---|---|
| [1] Ingestion | `vera/ingest.py` → `data/manifest.json` | yes |
| [2] Registry | `vera/registry.py` | **yes — never the LLM** |
| [3] Query resolution, gates, cache | `vera/pipeline.py` | yes |
| [3] Retrieval | `vera/retrieval.py` | yes (embeddings optional) |
| [4]/[5] Synthesis + verifier | `vera/backends/*`, prompts in `vera/prompts.py` | LLM (or mock) |
| [6] Presentation | `vera/cli.py`, `web/` | yes |
| [7] Escalation | `vera/escalation.py` | yes |
| Shared types | `vera/models.py` (`Passage`, `Retrieval`, `Usage`, `Result`) | — |
| Config | `vera/config.py` (`settings`, `.env` loader) | — |
| Change report | `vera/query.py` ("what changed in the latest version?") | yes |

`vera/__init__.py` exports everything **lazily** (`__getattr__`), so importing
`vera` pulls in no submodule, no optional SDK, and no protocol data.

---

## [1] Ingestion — `vera/ingest.py`

Two formats, one `ProtocolRecord` schema
(`doc_id, trial_id, title, version, effective_date, status, supersedes,
source_file, source_format, sections[], amendment_history, extra`):

- **Markdown** with YAML front-matter (parsed by a minimal in-house parser — no
  YAML dependency). Numbered headings (`## 5.2 …`, `## 6. …`) start sections;
  `—` in headings is normalised to `-`.
- **PDF** (pypdf): cover page and amendment metadata only. The real entinostat
  PDF has **no per-amendment change log**, and the record says so
  (`amendment_history.has_change_log = False`); `vera.query` then refuses to
  itemise changes rather than invent them. PDF records carry no sections.

`python -m vera.ingest [--check]` writes `data/manifest.json` (gitignored).
`pipeline.load_manifest()` builds it on demand if missing and caches it keyed on
file mtime.

## [2] Registry — `vera/registry.py`

Built from the front-matter of every `*.md` in `VERA_PROTOCOLS_DIR` (files
without `trial_id`/`version` — i.e. PDFs — are skipped).

**Governing rule:** `resolve_current_version(trial, as_of)` returns the version
with the latest `effective_date ≤ as_of` (tie-break on version). A version
governs from its own effective date until the next one takes effect.

- Returns `None` if the date precedes every version (→ `date_before_first_version`)
  or the trial is unknown (→ `unknown_trial`).
- `status` is **not** a filter. It records which version is active today and
  feeds `is_historical()` → `Result.historical` → the UI's "Historical view" banner.
- Trial ids normalise: `"Trial 412"`, `" 412 "` → `"412"`.

## [3] Retrieval — `vera/retrieval.py`

Conservative by design ("rather abstain than surface a boilerplate match"):

1. `content_terms()` — lowercase tokens minus stopwords (incl. domain words
   like *trial, protocol, patient, allowed, permitted*) and bare numbers.
2. If no term appears in **any** version → reason "topic not present in any
   version of the protocol".
3. IDF computed over the **governing version's** sections. A term is
   **salient** if it appears in ≥1 but ≤ half of the sections.
4. Section score = Σ idf × (heading_boost × heading hits + body hits);
   heading boost 3.0. Optional Voyage embeddings (`VOYAGE_API_KEY` + `voyageai`
   installed) add `2 × cosine` — keyword stays dominant.
5. A section is kept only if it matches a salient term. Confidence = IDF-weighted
   share of query terms present in the governing version ("term match" in the UI).
6. Keep passages ≥ `relative_floor` (0.2) × top score, take `top_k` (3).
7. `_conflicting_passages()` appends the same section id from superseded versions
   when the text differs (`status="superseded"`, `superseded_conflict=True`),
   oldest first — this powers change disclosure.

This is IDF keyword scoring, not BM25 (older docs said "BM25 + embeddings").

### Deterministic gates (in `answer_question`)

In order, each returns an abstention without calling a model:

1. No governing version (date before first version / unknown trial).
2. Governing version missing from the manifest (stale manifest).
3. `topic_in_current` false.
4. `confidence < VERA_LOW_CONFIDENCE` (0.34) or no passages.

Then synthesis + verify; if `verdict.grounded` or `verdict.citations_present` is
false → abstain with `unsupported` claims listed.

## [4]/[5] Backends — `vera/backends/`

| Backend | Transport | Default model | Notes |
|---|---|---|---|
| `mock` | none | — | Extractive, deterministic; verbatim passages + citations + change disclosure |
| `gemini` | urllib REST `generateContent` | `gemini-flash-latest` | `thinkingConfig.thinkingLevel`; JSON-schema verifier; rejects `RETIRED_FREE_TIER_MODELS`; retries once without `thinkingConfig` if a model rejects it |
| `openai` | urllib, `/chat/completions` | `gpt-4o-mini` | `OpenAICompatBackend`; on a 400 from a reasoning model, retries with `max_completion_tokens` instead of `max_tokens`; counts `reasoning_tokens` as thinking |
| `xai` | urllib, same dialect | `grok-3-mini` | `base_url = https://api.x.ai/v1` |
| `anthropic` | `anthropic` SDK (lazy import) | `claude-opus-4-5` | `thinking={"type": "adaptive"}` with `max_tokens` cap |

- `LLMBackend` interface: `synthesize(question, passages, governing) -> str`,
  `verify(question, answer, passages) -> dict`, `.usage`, `.model`.
- `get_backend(name, credentials)`: `auto` resolves via `_AUTO_ORDER =
  (gemini, anthropic, openai, xai)` then mock; a construction failure under
  `auto` degrades to mock, an explicit backend raises `FatalBackendError`.
- `Credentials(provider, api_key, model)` carries a browser-supplied key for one
  request; `fingerprint()` = first 12 hex chars of SHA-256 (cache key only).
- `validate_credentials()` lists the provider's models — zero tokens.
- Prompts (`vera/prompts.py`): `SYNTH_SYSTEM`, `VERIFY_SYSTEM`, JSON schemas,
  `format_passages()` clips each passage to `VERA_MAX_PASSAGE_CHARS`.

### Answer cache (`pipeline.py`)

Bounded LRU per process (`VERA_CACHE_SIZE`, 256; 0 disables). Key =
`(question.lower(), trial, as_of, backend, model, anthropic_model, key_fingerprint)`.
Hits return a copy with `usage.cached = True`, `elapsed_ms = 0`. Degraded
(fallback) results are never cached. Each gunicorn worker has its own cache.

## [6] Presentation

- **CLI** `vera/cli.py` (`python -m vera` / `vera` script). Flags: `--trial`
  (required), `--as-of`, `--backend`, `--json`, `--no-cache`, `-v`,
  `--escalate|--no-escalate`. Non-TTY stdin defaults to *not* escalating.
  Exit: 0 answered, 2 abstained, 1 error.
- **Web** `web/` — Flask app factory `create_app()`; `wsgi.py` for gunicorn;
  `python -m web` for dev.

| Route | Purpose |
|---|---|
| `GET /` | single-page UI (`templates/index.html`, `static/app.js`, `static/styles.css`) |
| `GET /healthz` | liveness/readiness; never calls an LLM |
| `GET /api/providers` | provider catalogue + which have server keys (never the keys) |
| `POST /api/validate-key` | zero-token key test (falls back to server key if none sent) |
| `GET /api/trials?as_of=` | trials, governing version, version timeline, first effective date |
| `GET /api/trials/<id>/sections` | headings of the governing version (topic hints) |
| `POST /api/ask` | `{question, trial, as_of?, backend?, provider?, api_key?, model?}` → `Result.to_dict()`; 502 + `kind: backend_config` on `FatalBackendError` |
| `POST /api/escalate` | `{question, trial, reason}` → sink result |
| `GET /robots.txt`, `/sitemap.xml` | crawler files with long TTL + `stale-if-error` |

Limits: question ≤ 2000 chars, key ≤ 512, model ≤ 128, body ≤ 64 KB.

- **Security** `web/security.py`: per-request CSP nonce (`script-src 'self'
  'nonce-…'`; `style-src` allows inline styles only), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`.
- **Front-end** `app.js` is deliberately "dumb": renders whatever `/api/ask`
  returns. Escapes via `esc()`; `addEventListener` only; credentials under a
  `localStorage` key; theme toggle; one-time "use your own API key" hint
  (`.model-hint[hidden]{display:none}` is required — author CSS beats the UA
  `[hidden]` rule); scroll-in footer (16px threshold).
- Static URLs are stamped `?v=<mtime>` by the `static_url()` template helper.

## [7] Escalation — `vera/escalation.py`

`build_work_item()` → provider-neutral dict; `to_ado_payload()` → ADO
create-work-item shape (`System.Title`, `System.Description`, tags
`VERA; escalation; abstention`). Sinks:

- `FileEscalationSink` (default): JSON to `VERA_ESCALATION_DIR`
  (`var/escalations/` locally, `/tmp/vera-escalations` on Render).
- `AdoMcpEscalationSink` (`VERA_ADO_MCP=1`, `ADO_PROJECT`): builds the payload;
  the actual `mcp_client.call_tool("wit_create_work_item", payload)` is a
  **commented stub** — the single production swap point.

---

## Cost and failure engineering

### The failure that shaped this

The first Gemini integration sent no `maxOutputTokens` and no thinking config,
and parsed `candidates[0]["content"]["parts"][0]["text"]`. Three API facts made
that a silent, billable dead end:

1. Reasoning is on by default and billed; thinking tokens count against
   `maxOutputTokens`.
2. Reasoning can consume the whole budget → `finishReason: MAX_TOKENS` with **no
   `parts`** → `KeyError` after the tokens were spent.
3. The pinned `gemini-2.0-flash` was retired from the free tier → every call
   returned `429 … limit: 0`, which looks like an exhausted quota.

A blanket `except Exception` fell back to the mock, so the user saw a plausible
answer, no error, and a shrinking balance.

### Defences

| Defence | Where | Prevents |
|---|---|---|
| Explicit output cap on every call | all real backends | unbounded output spend |
| Explicit `thinkingLevel` (default `low`) | `backends/gemini.py` | reasoning dominating the bill |
| Empty-candidate detection with named cause | `GeminiBackend._extract_text` | "tokens spent, nothing returned" |
| Retired-model rejection at construction | `RETIRED_FREE_TIER_MODELS` | phantom rate limits |
| `Retryable` vs `Fatal` split | `backends/base.py` | retrying a bad key against quota |
| Bounded exponential backoff | `with_retries` | retry storms |
| Passage truncation | `prompts.format_passages` | a big document inflating the prompt |
| LRU answer cache | `pipeline.py` | re-billing identical questions |
| Manifest cached on mtime | `pipeline.load_manifest` | re-parsing the PDF per request |
| Usage in CLI, API, UI | `models.Usage` | invisible spend |
| Loud degradation (`backend_error`, banner) | `pipeline.py`, UI | hidden fallbacks |

Caps are sized **with headroom**: setting them too low doesn't save money, it
produces empty answers after the tokens are spent.

### Measured cost

Trial 412 with `gemini-flash-latest`: ~2,500 tokens per uncached question across
2 calls (~1,900 in, ~300 out, ~300 thinking). Cached repeats cost nothing.
