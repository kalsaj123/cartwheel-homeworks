const listEl = document.getElementById("trace-list");
const detailEl = document.getElementById("trace-detail");
const countEl = document.getElementById("trace-count");
const refreshBtn = document.getElementById("refresh-btn");

let selectedId = null;

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// JSON.stringify already renders `null` explicitly for missing values, and we
// never omit a key on the backend, so nothing here silently disappears.
function jsonBlock(value) {
  return `<pre class="json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function fmtLatency(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(2)}s`;
}

async function fetchTraces() {
  const res = await fetch("/api/traces");
  if (!res.ok) throw new Error(`failed to list traces: ${res.status}`);
  return res.json();
}

async function fetchTrace(id) {
  const res = await fetch(`/api/traces/${id}`);
  if (!res.ok) throw new Error(`failed to load trace ${id}: ${res.status}`);
  return res.json();
}

function renderList(traces) {
  countEl.textContent = `${traces.length} trace${traces.length === 1 ? "" : "s"}`;
  if (traces.length === 0) {
    listEl.innerHTML = `<p class="muted" style="padding:12px">No traces found. Run some requests through the server first.</p>`;
    return;
  }
  listEl.innerHTML = "";
  for (const t of traces) {
    const row = document.createElement("div");
    row.className = "trace-row" + (t.trace_id === selectedId ? " selected" : "");
    row.dataset.id = t.trace_id;

    const badges = [];
    if (t.user_role) badges.push(`<span class="badge role">${escapeHtml(t.user_role)} #${escapeHtml(t.user_id ?? "?")}</span>`);
    if (t.tool_count) badges.push(`<span class="badge tools">${t.tool_count} tool call${t.tool_count === 1 ? "" : "s"}</span>`);
    if (t.has_permission_denied) badges.push(`<span class="badge denied">permission denied</span>`);
    if (t.has_error) badges.push(`<span class="badge error">error</span>`);

    row.innerHTML = `
      <div class="row-top">
        <span>${fmtTime(t.timestamp)}</span>
        <span>${fmtLatency(t.latency)}</span>
      </div>
      <div class="request-preview">${escapeHtml(t.request_preview || "(no captured input)")}</div>
      <div class="badges">${badges.join("")}</div>
    `;
    row.addEventListener("click", () => selectTrace(t.trace_id));
    listEl.appendChild(row);
  }
}

function renderObservation(obs) {
  const flagged = obs.level !== "DEFAULT" || obs.status_message;
  const det = document.createElement("details");
  det.className = "observation" + (flagged ? " flagged-error" : "");

  const typeBadge = `<span class="badge type-${escapeHtml(obs.type)}">${escapeHtml(obs.type)}</span>`;
  const errorBadge = flagged
    ? `<span class="badge error">${escapeHtml(obs.level)}${obs.status_message ? ": " + escapeHtml(obs.status_message) : ""}</span>`
    : "";
  const deniedAttr = obs.attributes && obs.attributes["cartwheel.permission_denied"] === "true";
  const deniedBadge = deniedAttr ? `<span class="badge denied">permission denied</span>` : "";

  det.innerHTML = `
    <summary>
      ${typeBadge}
      <span class="obs-name">${escapeHtml(obs.name || "(unnamed)")}</span>
      ${errorBadge}
      ${deniedBadge}
      <span class="obs-time">${fmtTime(obs.start_time)}</span>
    </summary>
    <div class="obs-body">
      <div class="obs-block">
        <div class="label">Input (arguments)</div>
        ${jsonBlock(obs.input)}
      </div>
      <div class="obs-block">
        <div class="label">Output (result)</div>
        ${jsonBlock(obs.output)}
      </div>
      <div class="obs-block">
        <div class="label">Attributes (raw span attributes, including cartwheel.* fields)</div>
        ${jsonBlock(obs.attributes)}
      </div>
      <div class="ids-line">
        id: ${escapeHtml(obs.id)} &middot; parent: ${escapeHtml(obs.parent_observation_id || "(root)")}
        &middot; start: ${escapeHtml(obs.start_time || "—")} &middot; end: ${escapeHtml(obs.end_time || "—")}
      </div>
    </div>
  `;
  return det;
}

function renderDetail(trace) {
  detailEl.innerHTML = "";

  const header = document.createElement("div");
  header.className = "detail-header";
  header.innerHTML = `
    <h2>Trace ${escapeHtml(trace.trace_id)}</h2>
    <div class="meta-line">Started ${fmtTime(trace.timestamp)} &middot; latency ${fmtLatency(trace.latency)} &middot; ${trace.observations.length} observations</div>
    <div class="meta-line">${trace.permalink ? `<a href="${escapeHtml(trace.permalink)}" target="_blank" rel="noopener">${escapeHtml(trace.permalink)}</a>` : "(no permalink)"}</div>
  `;
  detailEl.appendChild(header);

  const conv = document.createElement("div");
  conv.className = "conversation";
  conv.innerHTML = `
    <div class="turn"><div class="who">User</div><div class="text">${escapeHtml(extractText(trace.input) ?? "(no captured input)")}</div></div>
    <div class="turn"><div class="who">Assistant</div><div class="text">${escapeHtml(extractText(trace.output) ?? "(no captured output)")}</div></div>
  `;
  detailEl.appendChild(conv);

  const rawToggle = document.createElement("details");
  rawToggle.className = "raw-trace";
  rawToggle.innerHTML = `<summary>Raw trace JSON</summary>${jsonBlock(trace)}`;
  detailEl.appendChild(rawToggle);

  const sectionTitle = document.createElement("h3");
  sectionTitle.className = "section-title";
  sectionTitle.textContent = "Observations, chronological";
  detailEl.appendChild(sectionTitle);

  for (const obs of trace.observations) {
    detailEl.appendChild(renderObservation(obs));
  }
}

function extractText(messages) {
  if (!messages || !messages.length) return null;
  const parts = messages[0].parts || [];
  const textPart = parts.find((p) => p.type === "text");
  return textPart ? textPart.content : null;
}

async function selectTrace(id) {
  selectedId = id;
  document.querySelectorAll(".trace-row").forEach((row) => {
    row.classList.toggle("selected", row.dataset.id === id);
  });
  detailEl.innerHTML = `<p class="muted">Loading&hellip;</p>`;
  try {
    const trace = await fetchTrace(id);
    renderDetail(trace);
  } catch (err) {
    detailEl.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

async function load() {
  listEl.innerHTML = `<p class="muted" style="padding:12px">Loading traces&hellip;</p>`;
  try {
    const traces = await fetchTraces();
    renderList(traces);
  } catch (err) {
    listEl.innerHTML = `<p class="muted" style="padding:12px">${escapeHtml(err.message)}</p>`;
  }
}

refreshBtn.addEventListener("click", load);
load();
