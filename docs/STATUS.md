# Status — what's done, what's not, what's next

Snapshot as of **2026-11-13** (Windows → macOS migration). Version `1.0.0`.
Last code change: 2026-08-07. All 140 tests pass. Live at
https://vera.phiuture.com (Render, auto-deploys from `main`).

Keep this file current: update it when a feature lands or a limitation is found.

---

## Completed

**VERA 1.0 core (2026-07-25)**
- Ingestion of markdown + PDF protocols into one manifest schema.
- Deterministic registry with as-of resolution, `historical` flag, and distinct
  `date_before_first_version` / `unknown_trial` reasons.
- Conservative IDF keyword retrieval filtered to the governing version, with
  cross-version change disclosure; optional Voyage embeddings.
- Deterministic abstention gates + independent LLM verifier.
- Five backends (mock, Gemini, OpenAI, xAI, Anthropic) behind one interface,
  `auto` selection, retired-Gemini-model rejection.
- Cost controls: output caps, thinking level, retry taxonomy, bounded retries,
  passage truncation, LRU answer cache, per-question usage reporting.
- Loud degradation to the mock when a live backend fails.
- Human-confirmed escalation (file sink; ADO MCP sink payload).
- CLI (`python -m vera`), `vera.registry` and `vera.query` tools.
- Flask web UI: trial + as-of date picker bounded to the trial's lifetime,
  provenance, verifier verdict, usage, historical banner, escalation.
- Runtime provider selection from the browser (BYO key in `localStorage`,
  zero-token "Test key"), `VERA_ALLOW_CLIENT_KEYS` switch.
- Claude Code skill `protocol-qa`.
- Render blueprint + Dockerfile (non-root, baked manifest, healthcheck).

**Post-1.0 (2026-07-25 → 2026-08-07)**
- Strict per-request-nonce CSP and baseline security headers; tests proving
  browser keys are never logged/persisted/echoed.
- UI polish: tight-cropped logo and favicons (16/32/64, apple-touch), scroll-in
  copyright footer, one-time "use your own API key" callout.
- Static asset cache-busting via `?v=<mtime>`.
- Bug fixes verified in real Chrome: footer threshold unreachable on tall
  viewports (120px → 16px); hint `×` not hiding (`[hidden]` vs author CSS).
- SEO: `/robots.txt` and `/sitemap.xml` at root with crawler-friendly caching
  (`stale-if-error`) so idle cold starts don't make Google read "disallow all";
  canonical link; contradictory `Expires` header removed.
- Open Graph / Twitter card metadata with a Cloudinary preview image served as
  JPEG (LinkedIn's crawler wouldn't render WebP).
- README: problem statement, demo images, YouTube demo link, license/about.

---

## Known limitations (by design or accepted)

- **Escalation to ADO is a stub.** `AdoMcpEscalationSink.send` builds the exact
  payload but the `mcp_client.call_tool("wit_create_work_item", …)` line is
  commented out. Production escalations are therefore not filed anywhere real.
- **Escalations on Render are ephemeral** (`/tmp/vera-escalations`) — lost on
  restart/redeploy unless a Render disk is attached or the ADO sink is wired.
- **The PDF trial is not answerable.** `SNDX-275-0601` (entinostat) is ingested
  for metadata, but the registry only reads markdown front-matter and the PDF
  record has no sections — so `/api/trials` and `/healthz` list only `412` and
  `778`, and asking about the PDF trial returns `unknown_trial`. README's
  "Two trials, one system" table lists it, which can mislead.
- **Lexical retrieval.** Without embeddings, a question that shares no
  discriminative vocabulary with the protocol (synonyms, brand vs generic drug
  names) abstains. This errs on the safe side by design.
- **Per-process state.** Answer cache and manifest cache are per gunicorn
  worker; the registry is rebuilt from the markdown files on every request
  (cheap at this corpus size, not at hundreds of protocols).
- **Free Render plan** sleeps after 15 min idle (~50 s cold start).
- **Public BYO-key model:** a server-side key would answer for every anonymous
  visitor; the deployed instance relies on visitors bringing their own key or
  the mock. No authentication exists.
- **Server sees browser keys in memory** for the duration of a request
  (unavoidable — the server calls the provider).

## Open issues / inconsistencies found during migration

None block development. Application code was deliberately not changed during
the migration; these are candidates for a follow-up.

1. `README.md` says "`--backend auto` picks the first configured provider in
   that order" under a table ordered Gemini, OpenAI, xAI, Anthropic, but the code
   (`_AUTO_ORDER`) is **gemini → anthropic → openai → xai**. *(README line
   corrected in the migration commit.)*
2. `README.md` and `docs/DEPLOYMENT.md` quoted 135 tests; the suite has 140.
   *(Corrected in the migration commit.)*
3. `docs/ARCHITECTURE.md` was deleted in `f691010` but still linked from
   README, `render.yaml`, and `vera/config.py`. *(Restored in the migration commit.)*
4. `.claude/skills/protocol-qa/SKILL.md` says the demo dataset contains Trial
   412 only; Trial 778 also exists.
5. `tests/conftest.py` clears `GEMINI_/ANTHROPIC_/VOYAGE_API_KEY` but not
   `OPENAI_API_KEY`/`XAI_API_KEY`. Harmless today (`VERA_BACKEND=mock` is forced),
   but a test of `auto` resolution could pick up a developer's real key.
6. `VERA_PUBLIC_BASE_URL` (used by `web/routes.py`) is not documented in
   `.env.example` or `render.yaml`.
7. `ruff` is configured in `pyproject.toml` but not installed by
   `requirements-dev.txt`, and there is no CI workflow running tests or lint.
8. `voyageai` is an optional extra in `pyproject.toml` but absent from
   `requirements.txt` — embeddings stay off on Render even if
   `VOYAGE_API_KEY` is set.
9. Default Anthropic model is `claude-opus-4-5`; consider a newer default and
   keep `PROVIDERS` in `vera/backends/__init__.py` in sync with `config.py`.

## Next steps (candidates — confirm priority with the owner)

No roadmap was written down; these follow from the limitations above.

1. **Wire the ADO MCP client** in `AdoMcpEscalationSink.send` (the one
   production swap point) and add a test with a fake client.
2. **Make PDF protocols answerable**: section extraction for PDFs plus a way to
   register them (sidecar front-matter, or registry entries from the manifest).
3. Add a **CI workflow** (GitHub Actions: `pytest` + `ruff check`) and add ruff
   to `requirements-dev.txt`.
4. Fix the small inconsistencies in "Open issues" 4–9.
5. Persist escalations in prod (Render disk) until ADO is wired.
6. Optional: enable embeddings in prod (add `voyageai` to requirements) and
   measure abstention rate before/after.
7. Optional: authentication if a server-side key is ever configured on the
   public deployment.
