/* VERA web UI.
 *
 * Renders whatever /api/ask returns. Deliberately dumb: every protocol decision
 * (which version governs, whether to answer, whether the answer is grounded)
 * is made server-side by the pipeline. This file only presents it.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (id) => document.getElementById(id);

/** Escape before any interpolation into innerHTML. Protocol text is data. */
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c],
  );

const state = {
  trials: [],
  last: null,
  busy: false,
  providers: [],
  serverDefault: "mock",
  allowClientKeys: true,
};

/* ------------------------------------------------------- model credentials */
/* The provider/key/model chosen here are stored in this browser and sent with
 * each question. That is what lets a deployed instance switch provider or
 * rotate a key with no redeploy. The server holds nothing. */
const CREDS_KEY = "vera-credentials";

function loadCreds() {
  try {
    const raw = localStorage.getItem(CREDS_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) {
    /* corrupt or unavailable storage — fall through to the default */
  }
  return { provider: "", api_key: "", model: "" };
}

function saveCreds(creds) {
  try {
    localStorage.setItem(CREDS_KEY, JSON.stringify(creds));
  } catch (e) {
    /* private mode: settings simply do not persist across reloads */
  }
}

function clearCreds() {
  try {
    localStorage.removeItem(CREDS_KEY);
  } catch (e) {
    /* nothing to do */
  }
}

/** The credential fields to merge into an /api/ask body, if any are set. */
function credsPayload() {
  const c = loadCreds();
  const out = {};
  if (c.provider) out.provider = c.provider;
  if (c.api_key) out.api_key = c.api_key;
  if (c.model) out.model = c.model;
  return out;
}

/* ------------------------------------------------------------------ theme */
function initTheme() {
  const btn = el("theme-toggle");
  btn.addEventListener("click", () => {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("vera-theme", next);
    } catch (e) {
      /* private mode */
    }
  });
}

/* ------------------------------------------------------------ answer text */
/* A live model returns Markdown; the mock returns plain prose. Both go through
 * the same renderer so the two backends are visually indistinguishable. Input
 * is escaped first, so the "Markdown" we accept can never inject HTML. */

/** Wrap "(Trial 412 v3.0 §5.2, eff. 2025-03-01)" as a citation chip. */
function markCitations(html) {
  return html.replace(
    /\((Trial\s+[^()]*?v[\d.]+[^()]*?(?:eff\.?|effective)[^()]*?)\)/gi,
    (_, inner) => `<span class="cite-inline">${inner}</span>`,
  );
}

/** Inline spans: code, bold, italic, then citations. Escaped before any markup. */
function inline(text) {
  let h = esc(text);
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"); // before *italic*
  h = h.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  h = h.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  h = h.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  return markCitations(h);
}

/** Block-level Markdown: headings, bullet and numbered lists, paragraphs. */
function renderMarkdown(text) {
  const out = [];
  let para = [];
  let list = null;
  let listTag = "ul";

  const flushPara = () => {
    if (!para.length) return;
    out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    out.push(`<${listTag}>${list.join("")}</${listTag}>`);
    list = null;
  };
  const flushAll = () => {
    flushPara();
    flushList();
  };

  for (const raw of String(text).split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushAll();
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushAll();
      out.push(`<p class="answer-h">${inline(heading[2])}</p>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "ul" : "ol";
      if (list && listTag !== tag) flushList();
      listTag = tag;
      list = list || [];
      list.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      continue;
    }

    flushList();
    para.push(line);
  }
  flushAll();
  return out.join("");
}

/* Matches a change-disclosure label in either backend's phrasing:
 *   mock   -> "Change disclosure — this differs from the earlier ..."
 *   Gemini -> "**Change Disclosure:**\nThis reflects a change from ..."
 * The trailing (?:\*{1,2})? is what the previous version missed, which left a
 * stray "**" rendering literally at the start of the callout. */
const CHANGE_LABEL =
  /^\s*(?:#{1,6}\s*)?(?:\*{1,2}|__)?\s*change\s+disclosure\s*:?\s*(?:\*{1,2}|__)?\s*[—–:-]?\s*/i;

/** Turn a backend answer into paragraphs plus change-disclosure callouts. */
function renderAnswer(text) {
  const clean = String(text || "")
    .replace(/^\[[^\]]*\]\s*/, "")
    .trim();
  if (!clean)
    return `<p class="hint">The backend returned an empty answer.</p>`;

  const blocks = clean
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean);
  const out = [];

  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    if (!CHANGE_LABEL.test(block)) {
      out.push(renderMarkdown(block));
      continue;
    }

    let body = block.replace(CHANGE_LABEL, "").trim();
    // The label may sit alone on its own block, with the prose after a blank line.
    if (!body && i + 1 < blocks.length) body = blocks[++i];
    out.push(`<div class="change">
      <span class="change-label">Changed across versions</span>
      ${renderMarkdown(body)}
    </div>`);
  }
  return out.join("");
}

/* ------------------------------------------------------------- components */
function backendBadge(d) {
  const label = d.model ? `${d.backend} · ${d.model}` : d.backend;
  return `<span class="badge badge-muted" title="Synthesis backend">${esc(label)}</span>`;
}

/** Diagnostics strip: token spend, latency, and retrieval term match. */
function usageRow(d) {
  const u = d.usage || {};
  const bits = [];

  if (u.calls) {
    bits.push(`<span><b>${u.total_tokens}</b> tokens</span>`);
    bits.push(
      `<span><b>${u.calls}</b> model call${u.calls === 1 ? "" : "s"}</span>`,
    );
    if (u.thinking_tokens)
      bits.push(`<span><b>${u.thinking_tokens}</b> thinking</span>`);
  }
  if (u.cached)
    bits.push(`<span>served from cache — <b>0</b> new tokens</span>`);
  else if (d.elapsed_ms) bits.push(`<span><b>${d.elapsed_ms}</b> ms</span>`);

  if (typeof d.confidence === "number" && d.passages && d.passages.length) {
    const pct = Math.round(d.confidence * 100);
    bits.push(`<span title="Share of your question's topic-bearing words found in the governing version. VERA abstains below 34% rather than answer on a weak match.">
      <b>${pct}%</b> term match</span>`);
  }
  return bits.length ? `<div class="usage">${bits.join("")}</div>` : "";
}

/** Shown when the answer comes from a version that has since been superseded. */
function historicalNotice(d) {
  if (!d.historical) return "";
  const g = d.governing || {};
  return `<div class="notice notice-info">
    <span class="n-icon" aria-hidden="true">◷</span>
    <span><b>Historical view — this is not the current protocol.</b><br>
    You asked as of <b>${esc(d.as_of)}</b>, when <b>v${esc(g.version)}</b> was in force.
    It has since been superseded; reset the date to today to see what applies now.</span>
  </div>`;
}

function backendNotice(d) {
  if (!d.backend_error) return "";
  return `<div class="notice notice-warn">
    <span class="n-icon" aria-hidden="true">!</span>
    <span><b>Model backend unavailable — answered by the deterministic fallback.</b><br>
    ${esc(d.backend_error)}</span></div>`;
}

function provenance(passages) {
  if (!passages || !passages.length) return "";
  const rows = passages
    .map((p) => {
      const current = !p.superseded_conflict;
      return `<details class="src"${current ? " open" : ""}>
      <summary>
        <span class="tag ${current ? "tag-cur" : "tag-sup"}">${current ? "current" : "superseded"}</span>
        <span class="src-cite">${esc(p.citation)}</span>
      </summary>
      <p class="body">${esc(p.text)}</p>
    </details>`;
    })
    .join("");
  return `<div class="prov">
    <h4>Provenance — ${passages.length} passage${passages.length === 1 ? "" : "s"}</h4>
    ${rows}</div>`;
}

function answeredView(d) {
  const g = d.governing || {};
  const versionBadge = d.historical ? "badge-historical" : "badge-version";
  return `<div class="panel lg">
    ${historicalNotice(d)}
    ${backendNotice(d)}
    <div class="meta">
      <span class="badge ${versionBadge}"><span class="dot"></span>
        ${d.historical ? "In force on " + esc(d.as_of) : "Governing"}:
        Trial ${esc(g.trial)} v${esc(g.version)} · eff. ${esc(g.effective_date)}</span>
      <span class="badge badge-pass"><span class="dot"></span>Verifier passed</span>
      ${backendBadge(d)}
    </div>
    <div class="answer">${renderAnswer(d.answer)}</div>
    ${provenance(d.passages)}
    ${usageRow(d)}
  </div>`;
}

/* The abstention is caused by the *input*, not by the protocol or the system.
 * It gets its own view: no "escalate this" prompt, because there is nothing for
 * the protocol team to answer — the coordinator just needs a valid date. */
function inputIssueView(d) {
  const first = d.first_effective_date;
  const body =
    d.reason_kind === "date_before_first_version"
      ? `<p class="reason">You asked as of <b>${esc(d.as_of)}</b>, but Trial
         ${esc(d.trial)}'s first protocol version did not take effect until
         <b>${esc(first)}</b>. No protocol governed the trial before that date,
         so there is nothing to answer from.</p>
       <p class="ask">Pick a date on or after <b>${esc(first)}</b>.</p>
       <div class="actions">
         <button class="btn btn-primary" id="fix-date" type="button"
                 data-date="${esc(first)}">Use ${esc(first)}</button>
         <button class="btn btn-ghost" id="reset-date" type="button">Reset to today</button>
       </div>`
      : `<p class="reason">No trial with id <b>${esc(d.trial)}</b> is registered.
         Pick a trial from the list.</p>`;

  return `<div class="panel lg input-issue">
    <div class="head"><span class="icon" aria-hidden="true">?</span>
      Check the date you selected</div>
    ${body}
    <p class="hint" style="margin-top:14px">This is about the date in your request —
      VERA and the model backend are both working normally.</p>
  </div>`;
}

function abstainView(d) {
  const unsupported = (d.unsupported || []).length
    ? `<ul class="unsupported">${d.unsupported.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
    : "";
  return `<div class="panel lg abstain">
    ${historicalNotice(d)}
    ${backendNotice(d)}
    <div class="head"><span class="icon" aria-hidden="true">!</span>
      INSUFFICIENT EVIDENCE — escalating</div>
    <p class="reason"><b>Why:</b> ${esc(d.reason)}</p>
    ${unsupported}
    <p class="ask">VERA won't answer this from outside the protocol. Would you like to
      escalate it to the protocol team so they can follow up?</p>
    <div class="actions">
      <button class="btn btn-primary" id="escalate" type="button">Escalate to protocol team</button>
      <button class="btn btn-ghost" id="leave" type="button">Leave it</button>
    </div>
    <div id="esc-out"></div>
    ${provenance(d.passages)}
    ${usageRow(d)}
  </div>`;
}

function errorView(message) {
  return `<div class="panel lg"><div class="notice notice-error" style="margin:0">
    <span class="n-icon" aria-hidden="true">×</span>
    <span><b>Request failed.</b><br>${esc(message)}</span></div></div>`;
}

const skeletonView = `<div class="panel lg"><div class="skeleton">
  <div class="sk-line short"></div><div class="sk-line"></div>
  <div class="sk-line"></div><div class="sk-line mid"></div>
</div></div>`;

/* ---------------------------------------------------------------- sidebar */
function renderTimeline(trial) {
  const target = el("timeline");
  if (!trial || !trial.versions.length) {
    target.innerHTML = `<p class="hint">No versions registered.</p>`;
    return;
  }
  target.innerHTML = trial.versions
    .map((v) => {
      const cls = v.governing
        ? "is-governing"
        : v.status === "active"
          ? ""
          : "is-superseded";
      return `<div class="tl-row ${cls}">
      <span class="tl-rail" aria-hidden="true"><span class="tl-dot"></span></span>
      <span><span class="tl-ver">v${esc(v.version)}</span>
        <span class="tl-date"> · ${esc(v.effective_date)}</span></span>
      ${v.governing ? `<span class="tl-flag">governing</span>` : ""}
    </div>`;
    })
    .join("");
}

function currentTrial() {
  return state.trials.find((t) => t.id === el("trial").value) || null;
}

const TODAY = new Date().toISOString().slice(0, 10);

/** Bound the picker to the selected trial's lifetime, so "before any protocol
 *  existed" is unreachable by mistake rather than something to explain after. */
function applyDateBounds(trial) {
  const input = el("as-of");
  const first = trial && trial.first_effective_date;
  input.min = first || "";
  input.max = TODAY;
  el("date-floor").textContent = first
    ? `Trial ${trial.id} runs from ${first}.`
    : "Which version governed on this date.";
  // Switching trials can leave the date before the new trial's first version.
  if (first && input.value && input.value < first) input.value = first;
}

async function loadTrials() {
  const asOf = el("as-of").value;
  try {
    const r = await fetch(`/api/trials?as_of=${encodeURIComponent(asOf)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    state.trials = data.trials || [];
  } catch (e) {
    state.trials = [];
  }

  const sel = el("trial");
  const previous = sel.value;
  sel.innerHTML = state.trials.length
    ? state.trials
        .map((t) => `<option value="${esc(t.id)}">Trial ${esc(t.id)}</option>`)
        .join("")
    : `<option value="412">Trial 412</option>`;
  if (previous && state.trials.some((t) => t.id === previous))
    sel.value = previous;
  const trial = currentTrial();
  renderTimeline(trial);
  applyDateBounds(trial);
}

async function loadHealth() {
  try {
    const r = await fetch("/healthz");
    const d = await r.json();
    state.serverDefault = d.backend || "mock";
  } catch (e) {
    state.serverOffline = true;
  }
  refreshBackendBadge();
}

/* ------------------------------------------------------------------- ask */
async function ask() {
  if (state.busy) return;
  const question = el("question").value.trim();
  const trial = el("trial").value;
  const asOf = el("as-of").value;
  const out = el("result");

  if (!question) {
    el("question").focus();
    return;
  }

  state.busy = true;
  const btn = el("ask");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>Consulting protocol…`;
  out.innerHTML = skeletonView;

  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, trial, as_of: asOf, ...credsPayload() }),
    });
    const d = await r.json();

    if (!r.ok) {
      out.innerHTML = errorView(d.error || `HTTP ${r.status}`);
    } else {
      // The badge reports the *configured* provider and is left alone here;
      // what actually answered (including any fallback) is shown on the result
      // card. Overwriting it is what used to blank it to "unknown" on an
      // abstention, which read as an outage rather than a user input problem.
      state.last = {
        question: d.question,
        trial: d.trial,
        reason: d.reason || "",
      };
      if (d.reason_kind) out.innerHTML = inputIssueView(d);
      else out.innerHTML = d.answered ? answeredView(d) : abstainView(d);
    }
    out.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    out.innerHTML = errorView(String(e && e.message ? e.message : e));
  } finally {
    state.busy = false;
    btn.disabled = false;
    btn.textContent = "Ask VERA";
  }
}

/* ------------------------------------------------------------- escalation */
async function escalate(btn) {
  if (!state.last) return;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>Raising work item…`;
  const leave = el("leave");
  if (leave) leave.disabled = true;

  try {
    const r = await fetch("/api/escalate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.last),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);

    const dest =
      d.mode === "mcp"
        ? "Azure DevOps (via MCP)"
        : "var/escalations/ (demo file)";
    el("esc-out").innerHTML = `<div class="esc-result">
      <div class="ok">✓ Work item raised → ${esc(dest)}${d.path ? ` · ${esc(d.path)}` : ""}</div>
      <pre>${esc(JSON.stringify(d.ado_payload, null, 2))}</pre></div>`;
    btn.textContent = "Escalated";
  } catch (err) {
    el("esc-out").innerHTML =
      `<div class="notice notice-error" style="margin:14px 0 0">
      <span class="n-icon" aria-hidden="true">×</span>
      <span>Escalation failed: ${esc(err.message || err)}</span></div>`;
    btn.disabled = false;
    btn.textContent = "Escalate to protocol team";
    if (leave) leave.disabled = false;
  }
}

/* --------------------------------------------------------- model settings */
function providerById(id) {
  return state.providers.find((p) => p.id === id) || null;
}

/** Label + status dot in the top bar: the provider actually in effect now.
 *  Green = a real model will answer, amber = the deterministic mock,
 *  red = the server is unreachable. */
function refreshBackendBadge() {
  const chosen = loadCreds().provider || "";
  const effective = chosen && chosen !== "auto" ? chosen : state.serverDefault;
  const p = providerById(effective);

  el("backend-name").textContent = state.serverOffline
    ? "offline"
    : (p ? p.label.split(" (")[0] : effective);

  const dot = el("backend-badge").querySelector(".dot");
  dot.style.background = state.serverOffline
    ? "var(--danger)"
    : (effective === "mock" ? "var(--warn)" : "var(--ok)");

  el("backend-badge").title = chosen
    ? "Using this browser's provider setting — click to change"
    : "Using the server's configured provider — click to override";
}

function syncSettingsForm() {
  const id = el("provider").value;
  const p = providerById(id);
  const needsKey = !!(p && p.needs_key);

  el("key-field").hidden = !needsKey;
  el("model-field").hidden = id === "mock";
  el("provider-hint").textContent = p ? p.hint : "";

  const link = el("key-link");
  if (p && p.key_url) {
    link.href = p.key_url;
    link.hidden = false;
  } else {
    link.hidden = true;
  }

  el("model").placeholder = p && p.default_model
    ? `${p.default_model} (default)` : "provider default";
  el("model-hint").textContent =
    p && p.server_configured && needsKey
      ? "This provider already has a key on the server; leave the key blank to use it."
      : "Leave blank to use the default model.";
}

function openSettings() {
  const c = loadCreds();
  el("provider").value = c.provider || "";
  el("api-key").value = c.api_key || "";
  el("model").value = c.model || "";
  el("settings-status").innerHTML = "";
  syncSettingsForm();

  el("settings-backdrop").hidden = false;
  el("settings").hidden = false;
  el("provider").focus();
}

function closeSettings() {
  el("settings-backdrop").hidden = true;
  el("settings").hidden = true;
  el("backend-badge").focus();
}

function settingsStatus(kind, message) {
  const cls = { ok: "notice-info", err: "notice-error", warn: "notice-warn" }[kind];
  const icon = { ok: "✓", err: "×", warn: "!" }[kind];
  el("settings-status").innerHTML = `<div class="notice ${cls}" style="margin:14px 0 0">
    <span class="n-icon" aria-hidden="true">${icon}</span><span>${esc(message)}</span></div>`;
}

async function testKey(btn) {
  const provider = el("provider").value || state.serverDefault;
  const apiKey = el("api-key").value.trim();
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>Testing…`;
  try {
    const r = await fetch("/api/validate-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, api_key: apiKey }),
    });
    const d = await r.json();
    if (d.ok) {
      settingsStatus("ok", d.detail);
      // Offer the reachable models, so a wrong id is caught before it costs a call.
      if (d.models && d.models.length) fillModelSuggestions(d.models);
    } else {
      settingsStatus("err", d.detail || d.error || "Key check failed.");
    }
  } catch (e) {
    settingsStatus("err", String(e.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = "Test key";
  }
}

function fillModelSuggestions(models) {
  let list = el("model-options");
  if (!list) {
    list = document.createElement("datalist");
    list.id = "model-options";
    document.body.appendChild(list);
    el("model").setAttribute("list", "model-options");
  }
  list.innerHTML = models
    .map((m) => `<option value="${esc(m)}"></option>`)
    .join("");
  el("model-hint").textContent =
    `${models.length} models available to this key — start typing to pick one.`;
}

async function loadProviders() {
  try {
    const r = await fetch("/api/providers");
    const d = await r.json();
    state.providers = d.providers || [];
    state.serverDefault = d.server_default || "mock";
    state.allowClientKeys = d.allow_client_credentials !== false;
  } catch (e) {
    state.providers = [];
  }

  const sel = el("provider");
  const serverLabel = providerById(state.serverDefault);
  sel.innerHTML =
    `<option value="">Server default (${esc(serverLabel ? serverLabel.label.split(" (")[0] : state.serverDefault)})</option>` +
    state.providers
      .map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`)
      .join("");

  if (!state.allowClientKeys) {
    el("api-key").disabled = true;
    el("api-key").placeholder = "Disabled by this deployment";
  }
  refreshBackendBadge();
}

/* ------------------------------------------------------------ copyright bar */
/* Fades in once the user has scrolled a little, so it never competes with the
 * question box for attention on first paint. */
function initCopyrightBar() {
  const bar = el("copyright-bar");
  const THRESHOLD = 120;
  let ticking = false;
  const update = () => {
    const visible = window.scrollY > THRESHOLD;
    bar.classList.toggle("is-visible", visible);
    bar.setAttribute("aria-hidden", visible ? "false" : "true");
    ticking = false;
  };
  window.addEventListener("scroll", () => {
    if (!ticking) { requestAnimationFrame(update); ticking = true; }
  }, { passive: true });
  update();
}

/* --------------------------------------------------------- "add key" hint */
const HINT_DISMISSED_KEY = "vera-key-hint-dismissed";

/** A one-time pointer at the provider badge, so a first-time visitor discovers
 * they can bring their own key. Never shown again once dismissed, or once a
 * key has actually been saved. */
function initModelKeyHint() {
  const bubble = el("model-hint-bubble");
  let dismissed = false;
  try { dismissed = localStorage.getItem(HINT_DISMISSED_KEY) === "1"; }
  catch (e) { /* private mode: just show it, no persistence possible */ }

  if (dismissed || loadCreds().api_key) return;

  const dismiss = () => {
    bubble.hidden = true;
    try { localStorage.setItem(HINT_DISMISSED_KEY, "1"); } catch (e) { /* ignore */ }
  };

  setTimeout(() => { bubble.hidden = false; }, 900);
  el("model-hint-dismiss").addEventListener("click", (e) => {
    e.stopPropagation();
    dismiss();
  });
  el("backend-badge").addEventListener("click", dismiss, { once: true });
  window.addEventListener("scroll", dismiss, { once: true, passive: true });
  setTimeout(dismiss, 15000); // don't nag forever if ignored
}

function initSettings() {
  el("backend-badge").addEventListener("click", openSettings);
  el("settings-close").addEventListener("click", closeSettings);
  el("settings-backdrop").addEventListener("click", closeSettings);
  el("provider").addEventListener("change", syncSettingsForm);

  el("key-reveal").addEventListener("click", () => {
    const f = el("api-key");
    f.type = f.type === "password" ? "text" : "password";
  });

  el("test-key").addEventListener("click", (e) => testKey(e.currentTarget));

  el("save-settings").addEventListener("click", () => {
    saveCreds({
      provider: el("provider").value,
      api_key: el("api-key").value.trim(),
      model: el("model").value.trim(),
    });
    refreshBackendBadge();
    closeSettings();
    const bubble = el("model-hint-bubble");
    if (bubble) bubble.hidden = true;
    try { localStorage.setItem(HINT_DISMISSED_KEY, "1"); } catch (e) { /* ignore */ }
  });

  el("clear-key").addEventListener("click", () => {
    clearCreds();
    el("provider").value = "";
    el("api-key").value = "";
    el("model").value = "";
    syncSettingsForm();
    settingsStatus("ok", "Cleared — falling back to the server's configuration.");
    refreshBackendBadge();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !el("settings").hidden) closeSettings();
  });
}

/* ------------------------------------------------------------------- wire */
function init() {
  initTheme();
  el("as-of").value = TODAY;
  el("as-of").max = TODAY;

  el("ask").addEventListener("click", ask);
  el("question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      ask();
    }
  });
  el("trial").addEventListener("change", () => {
    const trial = currentTrial();
    renderTimeline(trial);
    applyDateBounds(trial);
  });
  el("as-of").addEventListener("change", loadTrials);

  document.querySelectorAll(".chip").forEach((c) => {
    c.addEventListener("click", () => {
      el("question").value = c.dataset.q;
      // Chips may target a specific trial, which is how the two "same question,
      // different trial" examples demonstrate corpus isolation.
      const wanted = c.dataset.trial;
      if (wanted && state.trials.some((t) => t.id === wanted)) {
        el("trial").value = wanted;
        const trial = currentTrial();
        renderTimeline(trial);
        applyDateBounds(trial);
      }
      el("question").focus();
    });
  });

  // Delegated: the abstention buttons are re-rendered on every result.
  document.addEventListener("click", (e) => {
    const target = e.target.closest("button");
    if (!target) return;
    if (target.id === "fix-date" || target.id === "reset-date") {
      el("as-of").value =
        target.id === "fix-date" ? target.dataset.date : TODAY;
      loadTrials().then(ask);
      return;
    }
    if (target.id === "escalate") escalate(target);
    if (target.id === "leave") {
      el("esc-out").innerHTML =
        `<p class="ask">No work item created — question left unanswered.</p>`;
      target
        .closest(".actions")
        .querySelectorAll("button")
        .forEach((b) => {
          b.disabled = true;
        });
    }
  });

  initSettings();
  initCopyrightBar();
  initModelKeyHint();
  // Providers first: the badge and the settings form both read from it.
  loadProviders().then(loadHealth);
  loadTrials();
}

document.addEventListener("DOMContentLoaded", init);
