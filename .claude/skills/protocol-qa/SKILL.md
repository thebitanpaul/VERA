---
name: protocol-qa
description: >-
  Answer a clinical-trial coordinator's question about what a protocol CURRENTLY
  requires — eligibility, concomitant medications, dosing, washout periods, visit
  timing — for a specific trial. Answers come only from the current governing
  protocol version, always with citations (trial, version, section, effective
  date), and disclose when a rule changed across versions. Use whenever a
  coordinator asks whether something is permitted, what a requirement is, or what
  changed. If the protocol does not cover the question, VERA abstains rather than
  guessing.
---

# Protocol Q&A (VERA)

This skill answers protocol questions by running the local VERA tool
(`python -m vera`). **You must not answer protocol questions from your own knowledge.**
Only relay what VERA returns. VERA reads the ingested protocol documents,
resolves which version currently governs the trial, retrieves the relevant
sections, and either answers with citations or abstains.

## When to use

Use this skill when a coordinator asks anything like:

- "Is <drug> permitted / allowed / excluded for trial <N>?"
- "What is the washout period / dose limit / visit window?"
- "What changed between protocol versions?"
- Any question about what the protocol currently requires.

Do **not** hand-write a protocol answer, and do not use general medical
knowledge — route every such question through VERA.

## How to run it

1. **Get the question and the trial number.** If the coordinator did not say
   which trial, ask. (The demo dataset contains Trial **412**.)

2. **Run VERA from the project root** with the Bash tool, always with
   `--no-escalate` on the first pass (this suppresses the tool's own prompt so
   you can handle escalation in the conversation instead):

   ```bash
   python -m vera "<the coordinator's question, verbatim>" --trial <N> --no-escalate
   ```

   Pass the question exactly as asked, in double quotes. Do not rephrase it.

3. **Read VERA's output and relay it** (see below). Keep every citation.

## How to read and relay the result

VERA prints one of two things.

### A. An answer

The output starts with the governing version, then a `[backend: ...]` line, then
the answer, a `Provenance:` block, and a `Verifier: PASSED` line. For example:

```
Trial 412: governing version v3.0 (effective 2025-03-01) as of 2026-07-24
[backend: mock]
Per Trial 412 v3.0 §5.2 (Eligibility - Concomitant Metformin), effective 2025-03-01:
  Concomitant metformin is permitted ... does not exceed 2000 mg/day ...
Change disclosure — this differs from the earlier Trial 412 v1.0 §5.2 ...
Provenance:
  [current]    Trial 412 v3.0 §5.2 ...
Verifier: PASSED — ...
```

Relay it to the coordinator in plain language, and **always keep**:

- the **answer** itself (e.g. "permitted, up to 2000 mg/day"),
- the **citation** — trial, version, section, and effective date,
- any **change disclosure** ("this changed from version 1.0, which excluded it").

Never drop the citation or the version — knowing *which version* the answer
comes from is the whole point.

### B. An abstention

If the output contains:

```
INSUFFICIENT EVIDENCE — escalating

Reason: <why>
```

then VERA could not answer safely. This happens when the topic is not in the
current protocol version, retrieval confidence is too low, or the answer could
not be grounded in a cited passage. **Do not fill the gap with your own
knowledge.**

Tell the coordinator, in plain language, that the question can't be answered from
the current protocol and give the reason. Then **ask them**:

> "I can't answer this from the current protocol. Would you like me to escalate
> it to the protocol team so they can follow up?"

- **If yes**, re-run the same command with `--escalate` instead of
  `--no-escalate`:

  ```bash
  python -m vera "<same question>" --trial <N> --escalate
  ```

  This files an Azure DevOps work item (via the ADO MCP server in production, or
  a JSON file under `var/escalations/` in this demo) summarising the question, trial,
  and reason. Confirm to the coordinator that it was raised.

- **If no**, do nothing further and confirm the question was left unanswered.

Always ask before escalating — this prevents a flood of low-value tickets.

## Notes

- Runs on a deterministic mock model by default (no setup needed). To use a real
  model, add a `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`) to a `.env` file; see
  `.env.example`. Backend selection does not change how you use this skill.
- If the output contains a `[warning]` line about the backend, the live model was
  unavailable and the deterministic fallback answered instead. The answer is
  still grounded in the protocol — mention the degradation, don't hide it.
- See `README.md` for example questions and expected results.
