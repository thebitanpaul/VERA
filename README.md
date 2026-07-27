<p align="center">
  <img src="https://res.cloudinary.com/b0tb1mho/image/upload/v1784753097/aj9rycbehzhpwepoc3g0.png" width="90" alt="VERA Logo">
</p>

<p align="center">
  <a href="https://vera.phiuture.com">
    <img src="https://img.shields.io/badge/Live-Demo-2563EB?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Live Demo">
  </a>
</p>

# VERA — Version-Explicit Retrieval & Answering


## Problem Statement

Clinical Research Organizations (CROs) manage dozens of clinical trials simultaneously. Each trial is governed by a **150–250 page protocol document** that defines patient eligibility, dosing schedules, washout periods, visit timelines, and safety procedures. These protocols are the single source of truth for how a trial must be conducted.

Hospital site staff frequently need quick answers to protocol-specific questions such as:

> - *What is the washout period for Cohort B?*
> - *Is a patient taking metformin eligible?*
> - *When should the follow-up visit occur after the last dose?*

Today, these questions are typically answered by a central support team over email or phone, creating delays that do not scale across multiple concurrent trials.

Building an intelligent protocol assistant is challenging because:

- **Protocol Versioning** – Clinical protocols evolve over time. Version 3 may supersede or contradict Version 1, while all historical versions remain available for audit and reference.
- **Patient Safety** – A confidently incorrect answer could lead to protocol deviations, incorrect dosing, or patient safety risks.

**VERA (Version-Explicit Retrieval & Answering)** addresses these challenges by deterministically identifying the governing protocol version, retrieving evidence only from that version, verifying every generated response, and abstaining whenever sufficient evidence is unavailable.

---

## Solution

VERA answers a trial coordinator's question about what a clinical-trial protocol
**currently requires**, using only the version that governs the trial today —
and it **abstains** rather than guessing when the protocol doesn't cover the
question.

<img src="https://res.cloudinary.com/b0tb1mho/image/upload/v1784995329/g8mytdgozkhwkvel5k4x.webp"/>

Every answer cites **trial, version, section, and effective date**. When a rule
changed across versions, VERA says so.

```
$ python -m vera "Is concomitant metformin permitted?" --trial 412 --no-escalate

Trial 412: governing version v3.0 (effective 2025-03-01) as of 2026-07-25

[backend: gemini · gemini-flash-latest]
Yes, concomitant metformin is permitted under the current protocol, provided the
total daily dose does not exceed 2000 mg/day and the patient has been on a stable
dose for at least 14 days prior to the first dose of VRA-412
(Trial 412 v3.0 §5.2, eff. 2025-03-01).

**Change Disclosure:**
Under Version 1.0 (eff. 2023-06-01) and Version 2.0 (eff. 2024-02-01), patients
taking metformin at screening were excluded from enrollment.

Provenance:
  [current]    Trial 412 v3.0 §5.2 (Eligibility - Concomitant Metformin), effective 2025-03-01
  [superseded] Trial 412 v1.0 §5.2 ...
  [superseded] Trial 412 v2.0 §5.2 ...

Verifier: PASSED — claims are grounded in the cited passages.

Tokens: 2474 total across 2 call(s) — in 1872, out 313, thinking 289 · 5675 ms
```

---
## Architecture

```text
┌───────────────────────────────────────────┐
   Protocol PDFs ─▶  [1] INGESTION & TAGGING
   (v1, v2, v3)        parse → chunk → attach metadata:
                       {trial_id, version, effective_date,
                        status, supersedes, section, cohort}
                                    │
                                    ▼
                       [2] PROTOCOL REGISTRY (the data product)
                       deterministic table: which version is
                       CURRENT/EFFECTIVE per trial as of a date
                                    │
   Coordinator Q ─▶  [3] QUERY RESOLVER
   "metformin ok      resolve trial_id (412) + as-of date
    for 412?"         → registry says current = v3.0
                                    │
                                    ▼
                       [4] HYBRID RETRIEVAL (BM25 + embeddings)
                       pre-filtered to v3.0 by default;
                       version history stays queryable
                                    │
                                    ▼
                       [5] GROUNDED SYNTHESIS (LLM)
                       answer ONLY from retrieved passages;
                       MUST cite version + section + effective date
                                    │
                                    ▼
                    ╔══════════════════════════════════════╗
                    ║ [6] VERIFIER ← the agentic layer    ║
                    ║ • every claim backed by a citation? ║
                    ║ • versions conflict unresolved?     ║
                    ║ • retrieval confidence < threshold? ║
                    ╚══════════════════════════════════════╝
                          │ pass                │ fail
                          ▼                     ▼
                 [7a] ANSWER            [7b] ABSTAIN
                         │                     │
                         └──────────► [8] ESCALATE
```
## Quick start

```bash
python -m venv venv
venv\Scripts\activate            # Windows;  source venv/bin/activate elsewhere
pip install -r requirements-dev.txt
python -m vera.ingest --check    # build the protocol manifest (once)
```

No API key is needed: with none configured, VERA runs a deterministic mock
backend that composes answers extractively from the retrieved passages.

**Web UI**

```bash
python -m web                     # → http://127.0.0.1:5000
```

**CLI**

```bash
python -m vera "<question>" --trial 412
```

| Flag | Effect |
|---|---|
| `--as-of YYYY-MM-DD` | ask as of a past date |
| `--backend auto\|mock\|gemini\|openai\|xai\|anthropic` | choose the synthesis backend |
| `--escalate` / `--no-escalate` | skip the escalation prompt |
| `--json` | emit the full structured result |
| `--no-cache` | bypass the in-process answer cache |
| `-v` | pipeline diagnostics on stderr |

**Tests**

```bash
pytest                            # 135 tests, no network, no key
```

---

## Using a real model

Copy `.env.example` to `.env` and add a key:

```dotenv
GEMINI_API_KEY=your-key-here      # https://aistudio.google.com/apikey
```

> **Leave `GEMINI_MODEL` unset.** The default `gemini-flash-latest` is a rolling
> alias to a served free-tier model. Pinning `gemini-2.0-flash`,
> `gemini-2.5-flash`, or any `gemini-1.5-*` gives a free-tier quota of **0**, so
> every call fails with a `429 … limit: 0` that reads like an exhausted key but
> is really a retired model. VERA rejects those ids at startup and tells you so.

Four providers are supported — set whichever key you have:

| Provider | Env var | Notes |
|---|---|---|
| Google Gemini | `GEMINI_API_KEY` | free tier available |
| OpenAI (GPT) | `OPENAI_API_KEY` | requires billing credit |
| xAI (Grok) | `XAI_API_KEY` | requires credit |
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | requires credit |

`--backend auto` picks the first configured provider in that order, else the mock.

**Or set nothing and choose in the browser.** The web UI has a **Model settings**
panel (click the provider badge in the top bar) where you pick a provider, paste
a key, and optionally name a model. The key is stored in that browser only and
sent with each question — so a deployed instance can switch provider or rotate a
key **with no redeploy**. "Test key" verifies it by listing the provider's models,
which costs no tokens.

### Cost

Two model calls per uncached question, **~2,500 tokens total** on the demo
corpus. Every call carries an explicit output cap and an explicit thinking
level, repeated questions are served from cache, and spend is reported in the
CLI, the API response, and the UI. See
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#cost-controls-in-production).

---

## Asking as of a past date

A version governs **from its own effective date until the next version takes
effect**. So `--as-of` reconstructs what a coordinator would have been told at
the time:

```bash
python -m vera "Is concomitant metformin permitted?" --trial 412 --as-of 2023-07-01
# -> governing version v1.0 ... metformin is EXCLUDED

python -m vera "Is concomitant metformin permitted?" --trial 412
# -> governing version v3.0 ... permitted up to 2000 mg/day
```

The result is flagged `historical` when the resolved version has since been
superseded, and the UI shows a *"Historical view — this is not the current
protocol"* banner so a past answer is never mistaken for current guidance.

A date **before the trial's first version** is a different thing entirely — no
protocol existed to govern, so there is nothing to answer from. That is reported
as an input problem (`reason_kind: date_before_first_version`), not as a protocol
gap, and the UI bounds its date picker to the trial's lifetime so it is hard to
reach by accident.

---

## Two trials, one system

The corpus holds two unrelated studies plus a real PDF protocol:

| Trial | Drug / indication | Versions |
|---|---|---|
| **412** | VRA-412 | v1.0, v2.0, v3.0 |
| **778** | NVK-778, plaque psoriasis | v1.0, v2.0 |
| SNDX-275-0601 | entinostat (real PDF) | v6.0 |

Both markdown trials have a **§5.2 Concomitant Metformin** and a **§6 Washout
Period, Cohort B** — with different rules. Asking the same question of each
proves retrieval never crosses trials:

```bash
python -m vera "Is concomitant metformin permitted?" --trial 412   # ≤ 2000 mg/day
python -m vera "Is concomitant metformin permitted?" --trial 778   # no dose ceiling
```

---

## Three example questions

Trial **412** is the primary demo dataset.

**1. Metformin — answered, with change disclosure**

```bash
python -m vera "Is concomitant metformin permitted?" --trial 412 --no-escalate
```

Permitted up to **2000 mg/day** per **v3.0 §5.2 (eff. 2025-03-01)**, and VERA
discloses that **v1.0 and v2.0 excluded it**.

**2. Cohort B washout — answered**

```bash
python -m vera "What is the washout period for Cohort B?" --trial 412 --no-escalate
```

A **14-day washout** before first dose, per **v3.0 §6**.

**3. Canagliflozin — abstains**

```bash
python -m vera "Is canagliflozin allowed?" --trial 412 --no-escalate
```

```
INSUFFICIENT EVIDENCE — escalating

Reason: topic not present in any version of the protocol
```

VERA then asks whether to escalate. On confirmation it files a work item
(question, trial, reason) — via the Azure DevOps MCP server in production, or as
JSON under `var/escalations/` in this demo. **It never escalates without being
asked**, which is what stops abstention turning into ticket spam.

---
## Demo

<img src="https://res.cloudinary.com/b0tb1mho/image/upload/v1784995821/iqa3lcu9sd7kt8y5m4lq.webp"/>
<img src="https://res.cloudinary.com/b0tb1mho/image/upload/v1784995822/ufe37mybok2flemob5dq.webp"/>

### Watch the demo video on youtube:
<a href="https://youtu.be/S3u0ZHKb9Xo" target="_blank">
<img src="https://img.youtube.com/vi/S3u0ZHKb9Xo/hqdefault.jpg" width="1080"/>
</a>

---

## As a Claude Code Skill

A non-technical coordinator can just ask in Claude Code. The `protocol-qa` skill
(`.claude/skills/protocol-qa/SKILL.md`) runs VERA, relays the answer with its
citations, and — if VERA abstains — asks whether to escalate before filing
anything.

---

## How it works

```
Protocol docs ──▶ [1] INGESTION ──▶ [2] REGISTRY ──▶ [3] RETRIEVAL
(md + pdf)          normalize          which version     current version only,
                    into one           governs, as of    + superseded sections
                    record schema      a date            on the same topic
                                                                │
                                              ┌─────────────────┘
                                              ▼
                          [4] SYNTHESIS ──▶ [5] VERIFIER ──▶ answer + provenance
                          answer only        independent          or
                          from passages      grounding audit  ABSTAIN ──▶ [6] ESCALATE
```

Steps [2] and the abstention gates are **deterministic** — the model never
decides which version governs or whether the evidence is sufficient. Only the
prose and the claim audit go through an LLM.

VERA abstains when: no version of the trial had taken effect by the date asked
about, the topic is absent from the governing version, retrieval confidence falls
below threshold, or the verifier cannot ground a claim in a cited passage.

**Retrieval confidence** ("term match" in the UI diagnostics) is the
IDF-weighted share of the question's topic-bearing words that appear in the
governing version. "Metformin" is rare and therefore heavily weighted; "dose" is
boilerplate and weighted near zero. Below **34%** VERA abstains rather than
answer on a weak lexical match — it is an abstention gate, not a confidence
score for the answer's correctness.

Full design notes in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Layout

```
vera/                   core library
  config.py             all tunables, env-overridable
  registry.py           [2] deterministic version resolution
  ingest.py             [1] markdown + PDF → one manifest schema
  retrieval.py          [3] keyword (+ optional embedding) retrieval
  prompts.py            shared prompts; passage truncation
  backends/             mock | gemini | openai | xai | anthropic, one interface
  pipeline.py           orchestration, gates, answer cache
  escalation.py         [6] file sink / ADO MCP sink
  cli.py                python -m vera
web/                    Flask app factory, routes, templates, static
data/protocols/         source documents
tests/                  135 tests
docs/                   ARCHITECTURE.md, DEPLOYMENT.md
wsgi.py                 gunicorn entry point
render.yaml             Render blueprint
```

## License

This project is licensed under the [phiUture Proprietary License](LICENSE).
See the LICENSE file for full license text.



## About phiUture


Welcome to phiUture — Beautiful Technology. Centered Around You. Shaping Tomorrow.

Inspired by the Golden Ratio (φ), our name represents beautiful, intelligent engineering, while the "U" stands for You—placing people at the center of every solution we build.

phiUture is an AI-first software company creating intelligent products, automation systems, and modern digital experiences. This channel documents the journey of building practical AI solutions, from concept to deployment.

```text
Here you'll find:
• AI applications and product demos
• AI agents and automation workflows
• Web and mobile app showcases
• Machine Learning and Data Engineering projects
• Product launches and development insights
• UI/UX and software engineering content
• Tutorials, experiments, and future innovations
```

| Personal | Business | Artist |
|----------|----------|--------|
| [![GitHub](https://img.shields.io/badge/GitHub-thebitanpaul-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/thebitanpaul) | [![Website](https://img.shields.io/badge/Website-phiUture-000000?style=for-the-badge&logo=googlechrome&logoColor=white)](https://phiuture.com) | [![YouTube](https://img.shields.io/badge/YouTube-thebitanpaul-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@thebitanpaul) |
| [![LinkedIn](https://img.shields.io/badge/LinkedIn-thebitanpaul-0A66C2?style=for-the-badge&logo=linkedin&logoColor=white)](https://linkedin.com/in/thebitanpaul) | [![YouTube](https://img.shields.io/badge/YouTube-phiUture-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@phiuture) | [![Spotify](https://img.shields.io/badge/Spotify-1DB954?style=for-the-badge&logo=spotify&logoColor=white)](https://open.spotify.com/artist/6ghDcCBlKzJIgm3e586jpV) |
| [![Instagram](https://img.shields.io/badge/Instagram-E4405F?style=for-the-badge&logo=instagram&logoColor=white)](https://instagram.com/thebitanpaul) | [![Google Play](https://img.shields.io/badge/Google_Play-Developer-34A853?style=for-the-badge&logo=googleplay&logoColor=white)](https://play.google.com/store/apps/dev?id=6358474525178045834&hl=en) | [![YouTube Music](https://img.shields.io/badge/YouTube_Music-FF0000?style=for-the-badge&logo=youtubemusic&logoColor=white)](https://music.youtube.com/playlist?list=OLAK5uy_km3cjEB2zl59Etcgv9UBKWw800O9G3NdE) |
| [![Facebook](https://img.shields.io/badge/Facebook-1877F2?style=for-the-badge&logo=facebook&logoColor=white)](https://facebook.com/thebitanpaul) | [![Email](https://img.shields.io/badge/Business_Email-thephiuture%40gmail.com-D14836?style=for-the-badge&logo=gmail&logoColor=white)](mailto:thephiuture@gmail.com) | [![Amazon Music](https://img.shields.io/badge/Amazon_Music-46C3D0?style=for-the-badge&logo=amazonmusic&logoColor=white)](https://music.amazon.com/albums/B0G52QMYDC) |
| [![X](https://img.shields.io/badge/X-000000?style=for-the-badge&logo=x&logoColor=white)](https://x.com/thebitanpaul_) |  | [![Apple Music](https://img.shields.io/badge/Apple_Music-FA243C?style=for-the-badge&logo=applemusic&logoColor=white)](https://music.apple.com/us/artist/thebitanpaul/1858534880) |
| [![Snapchat](https://img.shields.io/badge/Snapchat-FFFC00?style=for-the-badge&logo=snapchat&logoColor=000000)](https://snapchat.com/t/UgO0Iywr) |  | [![JioSaavn](https://img.shields.io/badge/JioSaavn-2BC5B4?style=for-the-badge&logo=jiosaavn&logoColor=white)](https://www.jiosaavn.com/artist/thebitanpaul-songs/zuo0NgC65gQ_) |
| [![Email](https://img.shields.io/badge/Personal_Email-thebitanpaul%40gmail.com-EA4335?style=for-the-badge&logo=gmail&logoColor=white)](mailto:thebitanpaul@gmail.com) |  |  |


---

<div align="center">

**2026 · © phiUture · All Rights Reserved**

</div>
