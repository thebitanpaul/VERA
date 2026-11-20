# Project context — goals, philosophy, requirements, decisions

This is the "why" behind VERA. For the "how", see [ARCHITECTURE.md](ARCHITECTURE.md);
for current state and next steps, see [STATUS.md](STATUS.md).

---

## The problem

Clinical Research Organizations run dozens of trials at once. Each is governed by
a 150–250 page **protocol** that defines eligibility, dosing, washout periods,
visit timing, and safety procedures. Hospital site staff (coordinators) constantly
need answers such as *"Is a patient on metformin eligible for trial 412?"* or
*"What is the washout for Cohort B?"*. Today a central team answers by email or
phone, which does not scale.

Two things make an assistant for this hard:

- **Versioning.** Protocols are amended. v3.0 may contradict v1.0, yet every
  version stays on file for audit. An answer from the wrong version is a
  protocol deviation.
- **Patient safety.** A confident wrong answer can mean wrong dosing or an
  ineligible patient enrolled. Silence is better than a guess.

## Goals

1. Answer from the **governing version only** — the one in force for that trial
   on the date asked about (today by default).
2. **Cite every answer**: trial, version, section, effective date.
3. **Disclose change**: if the same section said something different in an
   earlier version, say so.
4. **Abstain** when evidence is insufficient, with a reason a human can act on.
5. **Escalate on request** to the protocol team as a work item (Azure DevOps).
6. Run with **no API key** (deterministic mock) so it is reproducible anywhere,
   and with any of four real providers when a key is available.
7. Keep **LLM spend bounded and visible**.

## Philosophy

- **Determinism where safety lives.** Version resolution and the
  answer/abstain decision are rules, not model judgement. The model writes prose
  and audits claims; it never chooses the version and never overrides a gate.
- **When in doubt, abstain.** Retrieval tuning is deliberately conservative. A
  boilerplate word match ("dose", "day") is not evidence.
- **Degrade loudly, never silently.** A fallback answer is labelled as one. A
  hidden fallback is what once masked a billable failure (see ARCHITECTURE.md,
  "Cost and failure engineering").
- **The human stays in the loop for escalation.** Abstention never auto-files
  a ticket; otherwise abstention becomes ticket spam.
- **One code path.** CLI, web UI, and the Claude Code skill all run the same
  `answer_question` pipeline, so they cannot drift.
- **Configuration, not code, per deployment.** Every tunable is an env var.
- **Explain the why.** Code comments and commit messages record the real failure
  or constraint that motivated a choice. Keep doing this.

## Requirements

### Functional

- Resolve the governing version per trial for any as-of date; flag answers from
  a since-superseded version as `historical`.
- Distinguish input problems from protocol gaps: `date_before_first_version`
  and `unknown_trial` are separate `reason_kind`s from content abstention.
- Retrieve only from the governing version of the requested trial; add same-id
  sections from superseded versions when their wording differs.
- Verify the answer independently (grounded + citations present) before showing it.
- Escalate only on explicit confirmation; the work-item payload is identical
  across sinks (file in demo, ADO MCP in production).
- Ingest both markdown (front-matter) and PDF protocols into one manifest schema.
- Web UI: ask, pick trial and as-of date, see provenance/verifier/usage, pick a
  provider and paste a key (stored only in that browser), test a key at zero
  token cost, escalate.

### Non-functional

- Test suite runs **offline, keyless, and without writing into the repo**.
- Every LLM call: explicit output cap + thinking level; bounded retries for
  transient errors only; passages truncated; identical questions cached.
- Token usage reported per question in CLI, API, and UI.
- Browser-held keys: never logged/persisted/echoed; strict nonce CSP.
- Deployable as a single long-running process (Render free plan, 512 MB).
- Python ≥ 3.11; minimal dependencies (Flask, gunicorn, pypdf; `anthropic`
  optional; `voyageai` optional for embeddings).

## Architectural decisions (ADR summary)

| # | Decision | Why | Consequence |
|---|---|---|---|
| 1 | Registry resolves the governing version by **latest `effective_date` ≤ as-of**, not by `status` | `status` says what is current *today*; it says nothing about what governed on a past date | As-of queries reconstruct history; `status` is only used to label `historical` |
| 2 | Abstention gates are deterministic and run **before** any LLM call | Safety logic must not depend on a model; also saves tokens | Model only sees questions that already passed the gates |
| 3 | Retrieval = IDF-weighted keyword scoring with heading boost + "salient term" requirement; embeddings optional and blended weakly | Rare topic words (metformin) must dominate; boilerplate must not fake a match | Paraphrases with no shared vocabulary may abstain (see STATUS.md) |
| 4 | Pluggable `LLMBackend` (mock, gemini, anthropic, openai, xai); `auto` picks first configured in order gemini → anthropic → openai → xai → mock | Runs anywhere; provider choice is deployment config | Mock is the default in tests and with no keys |
| 5 | Gemini/OpenAI/xAI via `urllib` against REST, no SDKs | Fewer deps, full control of caps and error classification | Anthropic uses the SDK (imported lazily) |
| 6 | `Retryable` vs `Fatal` backend errors | Retrying a bad key or retired model burns quota and can't succeed | Only 429/5xx/timeouts are retried, bounded by `VERA_MAX_RETRIES` |
| 7 | Live-backend failure → mock fallback, flagged, not cached | Never block a coordinator, never hide a failure | UI banner + CLI warning; next request retries the real provider |
| 8 | Bring-your-own-key from the browser (`localStorage`), sent per request | Switch provider/rotate key with no redeploy; visitors spend their own quota | XSS is the main risk → strict nonce CSP, `esc()` everywhere, no inline handlers; `VERA_ALLOW_CLIENT_KEYS=0` disables |
| 9 | Escalation through an `EscalationSink`; file sink in demo, ADO MCP sink in prod | Payload stays identical; only transport changes | ADO MCP call is still a stub (see STATUS.md) |
| 10 | Single Flask service on Render; **no Vercel** | API and UI are one process; splitting adds CORS, a second cold start, and a rewrite | Free-plan cold starts; ephemeral disk for escalations |
| 11 | Manifest (`data/manifest.json`) is a build artefact, built at deploy and cached by mtime | First request must not parse the 1.2 MB PDF | Gitignored; rebuild locally with `python -m vera.ingest --check` |
| 12 | Static URLs stamped with `?v=<mtime>` | 1-hour static cache served stale JS/CSS after UI changes | No manual cache busting needed |
| 13 | `robots.txt`/`sitemap.xml` served with day-long TTL + `stale-if-error` | Google treats an unreachable robots.txt as "disallow all"; free plan idles | CDN keeps answering crawlers while the origin sleeps |

## Corpus

| Trial | Source | Versions | Notes |
|---|---|---|---|
| 412 (VRA-412, advanced solid tumours) | markdown | v1.0, v2.0, v3.0 (active, eff. 2025-03-01) | Primary demo. Metformin excluded in v1/v2, permitted ≤ 2000 mg/day in v3 |
| 778 (NVK-778, plaque psoriasis) | markdown | v1.0 (eff. 2024-09-15), v2.0 (active) | Same section ids as 412 with different rules — proves no cross-trial leakage |
| SNDX-275-0601 (entinostat) | real PDF | v6.0 / Amendment 5 | Ingested into the manifest for metadata only; **not in the registry**, so not answerable via ask (see STATUS.md) |

Synthetic trials are fictional. The demo questions with known outcomes:
metformin on 412 → answered with change disclosure; Cohort B washout on 412 →
14 days per v3.0 §6; canagliflozin on 412 → abstains ("topic not present in any
version").

## Branding / ownership

phiUture Tech (proprietary license). Public site `https://vera.phiuture.com`
(override with `VERA_PUBLIC_BASE_URL`). UI footer: "2026 | © phiUture | All
Rights Reserved". Open Graph/Twitter preview image is hosted on Cloudinary and
served as JPEG because LinkedIn's crawler would not render the WebP.
