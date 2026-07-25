┌───────────────────────────────────────────┐
   Protocol PDFs ─▶  [1] INGESTION & TAGGING                          │
   (v1, v2, v3)        parse → chunk → attach metadata:               │
                       {trial_id, version, effective_date,            │
                        status, supersedes, section, cohort}          │
                                    │                                 │
                                    ▼                                 │
                       [2] PROTOCOL REGISTRY  (the data product)      │
                       deterministic table: which version is          │
                       CURRENT/EFFECTIVE per trial as of a date       │
                                    │                                 │
   Coordinator Q ─▶  [3] QUERY RESOLVER                               │
   "metformin ok      resolve trial_id (412) + as-of date            │
    for 412?"         → registry says current = v3.0                  │
                                    │                                 │
                                    ▼                                 │
                       [4] HYBRID RETRIEVAL (BM25 + embeddings)       │
                       pre-filtered to v3.0 by default;               │
                       version history stays queryable                │
                                    │                                 │
                                    ▼                                 │
                       [5] GROUNDED SYNTHESIS (LLM)                   │
                       answer ONLY from retrieved passages;           │
                       MUST cite version + section + effective date   │
                                    │                                 │
                                    ▼                                 │
                    ╔══════════════════════════════════════╗         │
                    ║ [6] VERIFIER  ← the agentic layer     ║         │
                    ║  • every claim backed by a citation?  ║         │
                    ║  • versions conflict unresolved?      ║         │
                    ║  • retrieval confidence < threshold?  ║         │
                    ╚══════════════════════════════════════╝         │
                          │ pass                    │ fail            │
                          ▼                         ▼                 │
                 [7a] ANSWER + provenance    [7b] ABSTAIN             │
                 "Permitted, dose ≤2000mg    "I can't answer safely"  │
                  per 412 v3.0 §5.2,               │                  │
                  eff. 2025-03-01.                 ▼                  │
                  Note: v1.0 excluded it." [8] ESCALATE via MCP       │
                                            create ADO work item →    │
                                            central team queue        │
                         (every step logged for audit) ──────────────┘
---

## Module map

| Step | Module | Deterministic? |
|---|---|---|
| [1] Ingestion | `vera/ingest.py` | yes |
| [2] Registry | `vera/registry.py` | **yes — never the LLM** |
| [3] Query resolver | `vera/pipeline.py` | yes |
| [4] Retrieval | `vera/retrieval.py` | yes |
| [5] Synthesis | `vera/backends/` | LLM |
| [6] Verifier | gates in `vera/pipeline.py`, audit in `vera/backends/` | gates yes, audit LLM |
| [7] Presentation | `vera/cli.py`, `web/` | yes |
| [8] Escalation | `vera/escalation.py` | yes |

Configuration for all of it lives in `vera/config.py` and is environment-
overridable, so no deployment needs a code change.

---

## Implementation notes

This reference implementation follows the diagram above. A few steps are adapted
so the assessment is fully reproducible without external credentials.

**[5] Grounded synthesis — pluggable LLM backend.**
Synthesis and the verifier sit behind one swappable `LLMBackend` interface,
selected with `--backend` (default `auto`):

- `mock` (default when no key is set) — **the LLM call is *mimicked*
  programmatically in this demo.** The answer is composed extractively and
  verbatim from the retrieved passages (with inline citations and cross-version
  change disclosure), so runs are deterministic and need no network or API key.
  Swapping to a real model changes only which backend is selected.
- `gemini` — Google Gemini via `GEMINI_API_KEY` in `.env` (free tier works).
- `anthropic` — Claude via `ANTHROPIC_API_KEY` / `ant` login profile.

`auto` picks a real provider when its key is present, otherwise the mock.
Configuration is read from `.env` (see `.env.example`).

**[2] Registry — governing rule.**
A version governs from its own `effective_date` until the next version's
`effective_date`. Resolution therefore does **not** filter on `status`: asked as
of today it returns the `active` document, and asked as of a past date it returns
the version genuinely in force then. `status` records which document is current
*now* (validated at ingestion: exactly one `active` per trial) and is surfaced on
the result as `historical`, so a superseded answer is labelled rather than
silently presented as current guidance.

The one date with no answer is one preceding the trial's first version — nothing
governed yet. That is reported as `reason_kind: date_before_first_version`, a
distinct category from a protocol-content gap, because the fix belongs to the
caller (pick a valid date), not to the protocol team.

**[6] Verifier.** Deterministic gates run first (no version in force on the date
asked about → abstain; topic absent from the governing version → abstain;
retrieval confidence below threshold → abstain). Only if those pass does the LLM backend
run an independent, adversarial claim-grounding + citation audit; a failed audit
also abstains. The abstention string is exactly `INSUFFICIENT EVIDENCE —
escalating`.

**[7b]/[8] Abstain → confirm → escalate.**
To avoid over-escalation defeating VERA's purpose, abstention does **not**
auto-file a ticket. VERA first **asks the user** whether to escalate to the
designated team (default = leave the question). Flags `--escalate` /
`--no-escalate` override the prompt for automation; non-interactive sessions
default to *not* escalating.

On confirmed escalation, a provider-neutral work item (title, trial, question,
reason for abstention) is sent through a swappable `EscalationSink`:

- Production: `AdoMcpEscalationSink` — creates the item through the **ADO MCP
  server** (`wit_create_work_item`). This MCP call is the *only* line that
  changes for production. Selected when `VERA_ADO_MCP` is truthy.
- Demo (default): `FileEscalationSink` — writes the work item as JSON to
  `var/escalations/` and returns the exact ADO payload that *would* be sent to
  MCP.

The work-item payload is identical across sinks; only the transport differs.

---

## Cost and failure engineering

A protocol assistant that silently spends a budget and returns nothing is worse
than one that is plainly down. The following defences exist because the naive
implementation hit exactly that failure.

### The failure

The first Gemini integration sent no `maxOutputTokens` and no thinking
configuration, and parsed the response as
`candidates[0]["content"]["parts"][0]["text"]`. Three properties of the current
Gemini API turned that into a silent, billable dead end:

1. **Reasoning is on by default and is billed.** Thinking tokens are reported
   separately as `thoughtsTokenCount` but count against `maxOutputTokens`.
2. **Reasoning can consume the whole budget.** When it does, the candidate comes
   back with `finishReason: MAX_TOKENS` and **no `parts` key at all** — so the
   parse raised `KeyError` after the tokens were already spent.
3. **Retired model ids return `429 … limit: 0`.** The pinned default,
   `gemini-2.0-flash`, is no longer served on the free tier. Every request
   failed with what looks like an exhausted quota but is a model-selection bug.

A blanket `except Exception` then swallowed all of it and fell back to the mock,
so the user saw a plausible answer, no error, and a shrinking token balance.

### The defences

| Defence | Where | Prevents |
|---|---|---|
| Explicit `maxOutputTokens` on every call | `backends/gemini.py`, `backends/anthropic.py` | unbounded output spend |
| Explicit `thinkingConfig.thinkingLevel` | `backends/gemini.py` | reasoning dominating the bill |
| Empty-candidate detection with a named cause | `GeminiBackend._extract_text` | "tokens spent, nothing returned" |
| Retired-model rejection at construction | `RETIRED_FREE_TIER_MODELS` | phantom rate limits |
| `Retryable` vs `Fatal` error split | `backends/base.py` | retrying a bad key against quota |
| Bounded retries with exponential backoff | `with_retries` | retry storms |
| Passage truncation before prompting | `prompts.format_passages` | a large document inflating the prompt |
| Bounded LRU answer cache | `pipeline.py` | re-billing an identical question |
| Manifest cached on mtime | `pipeline.load_manifest` | re-parsing the PDF corpus per request |
| Usage surfaced in CLI, API, and UI | `models.Usage` | spend being invisible |

Two rules follow from the taxonomy:

- **A fatal error is never retried.** A bad key, a retired model, or a malformed
  request cannot succeed on attempt two; retrying only costs quota and latency.
- **A degraded answer is never cached.** When a live backend fails and the mock
  answers instead, the result is returned with `backend_error` set and is
  deliberately left out of the cache, so the next request tries the real
  provider again.

### Degrade loudly

When a live backend fails, VERA still answers — from the deterministic mock, so
a coordinator is never blocked by a provider outage. But the result carries
`backend_error`, the CLI prints a warning, and the UI shows a banner reading
*"Model backend unavailable — answered by the deterministic fallback."* The
previous behaviour, an unmarked fallback, is what made the original bug
invisible.

### Measured cost

On the Trial 412 corpus with `gemini-flash-latest`, one uncached question costs
**~2,500 tokens across 2 calls** (~1,900 in, ~300 out, ~300 thinking). Repeated
questions cost nothing.
