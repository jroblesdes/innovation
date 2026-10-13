/* Bed Placement Review: frontend logic.
 *
 * Talks to the FastAPI backend:
 *   GET /api/health
 *   GET /api/requests
 *   GET /api/requests/{transfer_id}
 *   GET /api/requests/{transfer_id}/placement[?refresh=true]
 *
 * If window.STATIC_DATA exists (static preview build), it is used instead.
 */

// Link to the source order. Use {hadm_id}, {subject_id}, {transfer_id} as placeholders.
// Leave empty until the PowerChart deep link is known.
const POWERCHART_URL_TEMPLATE = "";

const LABELS = {
  balloon_pump: "Balloon Pump",
  ventilator_et_trach: "Ventilator / ET / Trach",
  bariatric: "Bariatric",
  weight_over_300_lbs: "Weight over 300 lbs",
  fall_precautions: "Fall Precautions",
  contact_isolation: "Contact Isolation",
  bloodless: "Bloodless",
  decubiti: "Decubiti",
  immunosuppressed: "Immunosuppressed",
  lvad: "LVAD",
  tracheostomy: "Tracheostomy",
  video_ekg: "Video EKG",
  bmt: "BMT",
  ecmo: "ECMO",
  impella: "Impella",
  pregnant: "Pregnant",
  ventriculostomy: "Ventriculostomy",
};

/* ---------- data access ---------- */
const api = window.STATIC_DATA
  ? {
      health: async () => window.STATIC_DATA.health,
      requests: async () => window.STATIC_DATA.requests,
      detail: async (id) => window.STATIC_DATA.details[id],
      placement: async (id) => window.STATIC_DATA.placements[id],
    }
  : {
      health: () => getJSON("/api/health"),
      requests: () => getJSON("/api/requests"),
      detail: (id) => getJSON(`/api/requests/${encodeURIComponent(id)}`),
      placement: (id, refresh = false) =>
        getJSON(`/api/requests/${encodeURIComponent(id)}/placement${refresh ? "?refresh=true" : ""}`),
    };

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------- helpers ---------- */
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function setLabeled(id, label, value) {
  const node = $(id);
  node.replaceChildren(el("b", {}, `${label}:`), value ?? "");
  node.title = `${label}: ${value ?? ""}`;
}

function fmtDate(value) {
  if (!value) return "";
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? `${m[2]}/${m[3]}/${m[1]} ${m[4]}:${m[5]}` : value;
}

const genderLabel = (g) => ({ F: "Female", M: "Male" }[g] || "Unknown");
const shortUnit = (u) => (u || "").replace(/\s*\(.*\)\s*$/, "");

/* ---------- state ---------- */
let requests = [];
let currentId = null;
let health = {};

/* ---------- worklist ---------- */
function buildWorklist() {
  const select = $("request-select");
  select.replaceChildren(
    ...requests.map((r) =>
      el(
        "option",
        { value: r.transfer_id },
        `${r.subject_id} · ${r.department} · ${r.level_of_care} → ${r.requested_level_of_care} · ${shortUnit(r.careunit)}`
      )
    )
  );
  select.addEventListener("change", () => select.value && load(select.value));
  $("prev-btn").addEventListener("click", () => step(-1));
  $("next-btn").addEventListener("click", () => step(1));
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT") return;
    if (e.key === "ArrowLeft") step(-1);
    if (e.key === "ArrowRight") step(1);
  });
}

function step(delta) {
  const i = requests.findIndex((r) => r.transfer_id === currentId);
  const next = requests[i + delta];
  if (next) load(next.transfer_id);
}

function syncWorklist() {
  const i = requests.findIndex((r) => r.transfer_id === currentId);
  $("request-select").value = currentId;
  $("prev-btn").disabled = i <= 0;
  $("next-btn").disabled = i >= requests.length - 1;
  $("request-count").textContent = `${i + 1} of ${requests.length}`;
  history.replaceState(null, "", `#${currentId}`);
}

/* ---------- rendering: banner + clinical ---------- */
function renderDetail(d) {
  const p = d.patient, t = d.transfer, e = d.encounter, m = d.measurements;
  const name = `PATIENT ${p.subject_id}`;
  $("titlebar").textContent = name;
  document.title = `${name} · Bed Placement Review`;

  $("b-name").textContent = name;
  setLabeled("b-age", "Age", `${p.age} Years`);
  setLabeled("b-gender", "Gender", genderLabel(p.gender));
  $("b-allergies").textContent = "** Allergies Not Documented **";
  setLabeled("b-mrn", "MRN", p.subject_id);

  setLabeled("b-loc", "Loc", t.careunit);
  setLabeled("b-iso", "Isolation", "Loading…");
  $("b-iso").classList.remove("iso");
  setLabeled("b-service", "Service", t.service);
  setLabeled("b-dept", "Dept", t.department);
  setLabeled("b-fin", "Fin#", e.hadm_id);

  setLabeled("b-admit", "Inpatient |Admit Dt", `${fmtDate(e.admit_time)} Disch Dt:`);
  setLabeled("b-attending", "Attending", t.provider_name);
  setLabeled("b-code", "Code Status", "Not Documented");
  setLabeled("b-blood", "Blood Acceptable", "Not Documented");

  renderVitals(d.vitals);
  const loc = $("kv-loc");
  loc.replaceChildren(
    `${t.level_of_care} → `,
    el("span", { class: "req" }, t.requested_level_of_care)
  );

  const btn = $("source-btn");
  if (POWERCHART_URL_TEMPLATE) {
    btn.href = POWERCHART_URL_TEMPLATE
      .replace("{hadm_id}", e.hadm_id)
      .replace("{subject_id}", p.subject_id)
      .replace("{transfer_id}", t.transfer_id);
    btn.removeAttribute("aria-disabled");
    btn.title = "Open the source order in PowerChart";
  } else {
    btn.href = "#";
    btn.setAttribute("aria-disabled", "true");
    btn.title = "PowerChart link not configured yet";
  }
}

// Show each vital where the chart has it; "–" (null) where it doesn't.
const VITALS = {
  heart_rate: (v) => `${v.value}`,
  blood_pressure: (v) => `${v.value}`,
  respiratory_rate: (v) => `${v.value}`,
  temperature: (v) => `${v.value}°C`,
  oxygen_saturation: (v) => `${v.value}%`,
  weight: (v) => `${v.value} lb`,
};

function renderVitals(vitals) {
  for (const [key, format] of Object.entries(VITALS)) {
    const cell = $(`v-${key}`);
    const v = vitals ? vitals[key] : null;
    if (v == null) {
      cell.textContent = "–";
      cell.classList.add("null");
      cell.title = "Not charted";
    } else {
      cell.textContent = format(v);
      cell.classList.remove("null");
      cell.title = `${format(v)} · ${v.source}, ${fmtDate(v.time)}`;
    }
  }
}

function setLoadingPlacement() {
  $("summary-text").replaceChildren(el("span", { class: "loading" }, "Generating AI summary…"));
  $("summary-source").textContent = "";
  $("care-needs").replaceChildren();
  $("needs-count").textContent = "";
  $("not-flagged").replaceChildren();
  $("notflagged-count").textContent = "";
  for (const id of ["kv-drips", "kv-lines", "kv-tubes"]) $(id).textContent = "–";
  $("bed-warnings").replaceChildren();
  $("beds").replaceChildren(el("li", { class: "empty-beds loading" }, "Finding beds…"));
  $("bed-footer").replaceChildren();
  $("routing-note").textContent = "best match first";
}

/* ---------- rendering: summary, needs, beds ---------- */
function renderPlacement(pl) {
  const s = pl.summary;

  // summary
  $("summary-text").textContent = s.text;
  $("summary-source").textContent = window.STATIC_DATA
    ? "Preview data: sample summary. The live site uses the AI summary."
    : `AI summary (${s.model}) · informational only, verify in chart`;

  // banner isolation + blood
  const iso = s.flagged.includes("contact_isolation");
  setLabeled("b-iso", "Isolation", iso ? `Contact – ${s.contact_isolation_reason}` : "None Flagged");
  $("b-iso").classList.toggle("iso", iso);
  setLabeled("b-blood", "Blood Acceptable", s.bloodless === "yes" ? "No (Bloodless)" : "Not Documented");

  // care needs
  const needs = $("care-needs");
  if (s.flagged.length) {
    needs.replaceChildren(
      ...s.flagged.map((f) =>
        el(
          "li",
          {},
          LABELS[f] || f,
          f === "contact_isolation" && s.contact_isolation_reason
            ? el("span", { class: "reason" }, `– ${s.contact_isolation_reason}`)
            : null
        )
      )
    );
    $("needs-count").textContent = `${s.flagged.length} flagged`;
  } else {
    needs.replaceChildren(el("li", { class: "none" }, "No special care needs flagged"));
    $("needs-count").textContent = "";
  }

  // not flagged
  $("not-flagged").replaceChildren(...s.not_flagged.map((f) => el("li", {}, LABELS[f] || f)));
  $("notflagged-count").textContent = `${s.not_flagged.length} attributes`;

  // drips / lines / tubes
  const fill = (id, items) => {
    const node = $(id);
    node.textContent = items && items.length ? items.join(", ") : "None documented";
    node.classList.toggle("empty", !(items && items.length));
  };
  fill("kv-drips", s.drips);
  fill("kv-lines", s.lines);
  fill("kv-tubes", s.tubes);

  renderBeds(pl.recommendations);
}

function renderBeds(r) {
  if (r.routing) {
    const units = r.routing.tiers.map((t) => t.units.join(", ")).filter(Boolean).join(" → ");
    $("routing-note").textContent = `${r.routing.requested_level_of_care} · ${units || "no units on board"}`;
  }

  $("bed-warnings").replaceChildren(
    ...r.warnings.map((w) => el("div", { class: "bed-warning" }, el("span", { "aria-hidden": "true" }, "⚠"), w))
  );

  const list = $("beds");
  if (!r.beds.length) {
    list.replaceChildren(el("li", { class: "empty-beds" }, r.message || "No matching beds."));
  } else {
    list.replaceChildren(
      ...r.beds.map((b, i) =>
        el(
          "li",
          { class: `bed${i === 0 ? " top" : ""}` },
          el(
            "div",
            { class: "bed-name" },
            el("span", { class: "rank" }, i + 1),
            `${b.unit} · Room ${b.room} Bed ${b.bed}`,
            el("span", { class: `tier-tag${b.tier === "Overflow" ? " overflow" : ""}` }, b.tier)
          ),
          el("ul", { class: "matches" }, b.checks.map((c) => el("li", {}, c))),
          b.cautions.length ? el("ul", { class: "cautions" }, b.cautions.map((c) => el("li", {}, c))) : null
        )
      )
    );
  }

  const foot = [];
  if (r.total_matches > r.beds.length) {
    foot.push(`${r.total_matches} matching beds; ${r.total_matches - r.beds.length} more not shown.`);
  }
  const ex = r.excluded || {};
  const exParts = [];
  if (ex.isolation) exParts.push(`${ex.isolation} for isolation conflicts`);
  if (ex.sex) exParts.push(`${ex.sex} for roommate sex`);
  if (exParts.length) foot.push(`Skipped ${exParts.join(", ")}.`);
  if (r.dirty_beds && r.dirty_beds.length) {
    foot.push(`Waiting on EVS: ${r.dirty_beds.join(", ")}.`);
  }
  foot.push(...(r.notes || []));
  $("bed-footer").replaceChildren(...foot.map((t) => el("p", {}, t)));
}

function renderError(err) {
  $("summary-text").replaceChildren(el("span", { class: "error" }, err.message));
  $("summary-source").textContent = "Use Refresh to try again.";
  $("beds").replaceChildren(el("li", { class: "empty-beds error" }, "Recommendations unavailable."));
}

/* ---------- load ---------- */
async function load(id, { refresh = false } = {}) {
  currentId = id;
  syncWorklist();
  setLoadingPlacement();
  try {
    const detail = await api.detail(id);
    if (currentId !== id) return;
    renderDetail(detail);
    const placement = await api.placement(id, refresh);
    if (currentId !== id) return;
    renderPlacement(placement);
  } catch (err) {
    if (currentId === id) renderError(err);
  }
}

async function init() {
  $("source-btn").addEventListener("click", (e) => {
    if ($("source-btn").getAttribute("aria-disabled") === "true") e.preventDefault();
  });
  $("refresh-btn").addEventListener("click", () => currentId && load(currentId, { refresh: true }));
  if (window.STATIC_DATA) $("refresh-btn").hidden = true;

  try {
    [health, requests] = await Promise.all([api.health(), api.requests()]);
  } catch (err) {
    $("b-name").textContent = "Backend not reachable";
    renderError(err);
    return;
  }
  buildWorklist();
  const fromHash = location.hash.slice(1);
  const start = requests.find((r) => r.transfer_id === fromHash) || requests[0];
  if (start) load(start.transfer_id);
}

init();
