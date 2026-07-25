# Deploying VERA

## Which platform: Render alone

**Render alone. Do not add Vercel.**

VERA is a single Flask process that serves the API *and* renders the UI from the
same templates. Splitting it would mean:

| | Render only | Vercel + Render |
|---|---|---|
| Services to run | 1 | 2 |
| CORS config | none | required |
| Front-end rewrite | none | port `web/templates` + `static` to a JS app |
| Cold-start hops | 1 | 2 (Vercel edge → Render) |
| Escalation file sink | works | works (still on Render) |
| Cost | free tier | free tier ×2, for no gain |

Vercel would earn its place if the front-end became a Next.js app or needed edge
caching. Today it is one HTML template and two static files that Flask already
serves — a second platform buys nothing.

> Vercel's Python runtime *can* host Flask, but its filesystem is read-only
> apart from `/tmp`, and its functions are short-lived. The `FileEscalationSink`
> and the in-process answer cache both assume a persistent process. Render's
> long-running service fits without changes.

---

## Deploy to Render

### Step 1 — Push to GitHub

```bash
cd C:\Users\BitanPaul\Documents\VERA
git init
git add .
git commit -m "VERA 1.0"
git branch -M main
git remote add origin https://github.com/thebitanpaul/VERA.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `venv/`, `var/`, and the generated
manifest. **Confirm your key is not staged** before pushing:

```bash
git ls-files | grep -x ".env"     # must print nothing
```

### Step 2 — Create the Render service

1. Go to <https://dashboard.render.com> and sign in with GitHub.
2. **New +** → **Blueprint**.
3. Pick the `thebitanpaul/VERA` repository. Authorise Render if prompted.
4. Render detects [`render.yaml`](../render.yaml) and shows one web service
   named `vera`. Click **Apply**.

It provisions with:

- **Build**: `pip install -r requirements.txt && python -m vera.ingest --check`
- **Start**: `gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
- **Health check**: `/healthz`

Building the manifest at deploy time means the first request never pays to parse
the 1.2 MB PDF. First deploy takes roughly 3–5 minutes.

### Step 3 — Set the API key (optional)

`render.yaml` marks `GEMINI_API_KEY` as `sync: false`, so Render prompts for it
during **Apply** rather than reading it from the repo. Paste the key from
<https://aistudio.google.com/apikey>, or leave it blank.

**You are not locked in either way.** The UI has a **Model settings** panel
(click the provider badge in the top bar) where any visitor can choose Gemini,
OpenAI, xAI, Anthropic, or the mock and paste their own key. That key lives in
their browser and is sent per request — so switching provider or rotating a key
**needs no redeploy**. See [Runtime provider selection](#runtime-provider-selection).

With no key anywhere, the service runs the deterministic mock backend.

Leave `GEMINI_MODEL` unset — see [Model selection](#model-selection) below.

### Step 4 — Verify

```bash
curl https://vera-xxxx.onrender.com/healthz
```

```json
{"status":"ok","version":"1.0.0","trials":["412","778"],"manifest":true,"backend":"gemini"}
```

If `backend` reads `mock` when you expected `gemini`, the key did not reach the
process — check **Environment** in the Render dashboard.

> **Free-plan cold starts.** A free Render service sleeps after 15 minutes idle
> and takes ~50 seconds to wake. For a demo you are showing live, either upgrade
> to Starter ($7/mo, no sleep) or hit the URL a minute beforehand.

---


## Runtime provider selection

The deployed app does not need a redeploy to change model provider or key.

Clicking the provider badge in the top bar opens **Model settings**:

| Control | Effect |
|---|---|
| Provider | `Server default`, Mock, Gemini, OpenAI (GPT), xAI (Grok), Anthropic |
| API key | Sent with each question; stored in that browser's `localStorage` only |
| Model | Optional override; blank uses the provider default |
| Test key | Lists the provider's models — **consumes no tokens** |

How the key is handled:

- It lives **only in that browser's `localStorage`** — the server never writes
  it to disk, never logs it, and never includes it in an API response (checked
  by `tests/test_credentials.py`).
- It reaches the answer cache only as a **12-character SHA-256 prefix**, so two
  sessions with different keys cannot read each other's cached answers.
- It travels in the request body over HTTPS. Render terminates TLS for you.
- The server still sees the plaintext key **in memory, for the duration of that
  one request** — that is unavoidable, since it is VERA who calls Gemini/OpenAI/
  xAI/Anthropic on the visitor's behalf. Nothing about the request is persisted
  after the response is sent.

Because the key sits in `localStorage`, the main residual risk is **XSS**: any
script an attacker got to run on the page could read it. Two things narrow that
risk to "an attacker would need to find an actual injection bug":

- Every server response carries a **strict, per-request-nonce Content-Security-
  Policy** (`web/security.py`) — `script-src 'self' 'nonce-<random>'`, no
  blanket `'unsafe-inline'`. A `<script>` tag smuggled in through any future bug
  would be refused by the browser; only the one inline script VERA itself
  renders (the theme-flash guard) carries the matching nonce.
- All UI rendering escapes interpolated text before it reaches `innerHTML`
  (`esc()` in `app.js`) and uses `addEventListener` exclusively — there are no
  inline `onclick=` handlers for an injection to piggyback on.

`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and
`X-Content-Type-Options: nosniff` are set on every response as further baseline
hardening.

To lock a deployment to its own server-side credentials and refuse
browser-supplied keys entirely, set `VERA_ALLOW_CLIENT_KEYS=0`.

> Because keys sit in the visitor's browser, a public deployment lets anyone
> spend **their own** quota, not yours. If you set a server-side key instead,
> that key answers for every anonymous visitor — put the service behind auth, or
> leave the server key unset and let people bring their own.

---

## Model selection

**Leave `GEMINI_MODEL` unset.** The default is `gemini-flash-latest`, a rolling
alias to a currently served free-tier flash model.

Pinning a retired id is the trap this project already hit:

```
429 RESOURCE_EXHAUSTED
  Quota exceeded for metric: generate_content_free_tier_requests,
  limit: 0, model: gemini-2.0-flash
```

`limit: 0` is not an exhausted quota — it means that model is **not served on
the free tier at all**. `gemini-2.0-flash`, `gemini-2.5-flash`, and the
`gemini-1.5-*` family are all in this state for keys issued after their
retirement. VERA rejects these ids at startup with an explanatory error rather
than letting every request fail as a phantom rate limit.

---

## Cost controls in production

Two model calls per uncached question (synthesis + verification). Measured on
the Trial 412 corpus with `gemini-flash-latest`: **~2,500 tokens per question**,
of which ~300 are thinking tokens.

| Setting | Default | What it protects against |
|---|---|---|
| `VERA_SYNTH_MAX_TOKENS` | 2048 | unbounded synthesis output |
| `VERA_VERIFY_MAX_TOKENS` | 1024 | unbounded verifier output |
| `VERA_THINKING_LEVEL` | `low` | reasoning tokens dominating the bill |
| `VERA_MAX_PASSAGE_CHARS` | 4000 | a large source document inflating the prompt |
| `VERA_MAX_RETRIES` | 2 | a rate-limited provider becoming a retry storm |
| `VERA_CACHE_SIZE` | 256 | re-billing an identical repeated question |
| `VERA_ALLOW_CLIENT_KEYS` | `1` | set to `0` to refuse browser-supplied keys |

`maxOutputTokens` **includes thinking tokens** on current Gemini models, so the
caps are sized with headroom rather than trimmed to the expected answer length.
Setting them too low does not save money — it produces empty answers after the
tokens are already spent.

Token spend is reported per question: in `/api/ask` responses (`usage`), on the
CLI, and in the UI footer.

### Scaling the workers

`--workers 2 --threads 4` handles 8 concurrent questions. Each worker holds its
own answer cache and manifest, so raising `--workers` multiplies memory and
lowers the cache hit rate. On the free plan (512 MB) do not exceed 2 workers;
raise `--threads` instead, since the workload is I/O-bound on the model call.

---

## Escalations in production

The default `FileEscalationSink` writes JSON to `VERA_ESCALATION_DIR`
(`/tmp/vera-escalations` on Render). **Render's filesystem is ephemeral** —
those files are lost on restart or redeploy.

Two production options:

1. **Attach a Render disk** and point `VERA_ESCALATION_DIR` at its mount path.
2. **Switch to the ADO MCP sink** — set `VERA_ADO_MCP=1` and `ADO_PROJECT=<your
   project>`. The work-item payload is identical; only the transport changes.
   One line in `vera/escalation.py` (`AdoMcpEscalationSink.send`) needs the MCP
   client wired in.

Option 2 is the intended production path: escalations belong in the team's work
queue, not on a web server's disk.

---

## Container deploy (alternative)

A [`Dockerfile`](../Dockerfile) is included for Render's Docker runtime, Fly.io,
Cloud Run, or any container host. It bakes the manifest into the image, runs as
a non-root user, and exposes the same `/healthz` check.

```bash
docker build -t vera .
docker run -p 8000:8000 -e GEMINI_API_KEY=... vera
```

To use it on Render, change `runtime: python` to `runtime: docker` in
`render.yaml` and drop the build/start commands.

---

## Pre-deploy checklist

```bash
pytest                                   # 135 tests, no network required
python -m vera.ingest --check            # manifest builds, no warnings
python -m vera "Is concomitant metformin permitted?" --trial 412 --no-escalate
curl localhost:5000/healthz              # after: python -m web
git ls-files | grep -x ".env"            # must print nothing
git ls-files | xargs grep -l "AQ\.\|sk-\|AIza" 2>/dev/null   # no keys committed
```
