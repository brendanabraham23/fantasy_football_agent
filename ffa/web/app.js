/* Fantasy Analyzer UI: hash-routed tabs over the /api JSON endpoints. No build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const S = { summary: null, summaryErr: null, pool: null, news: null, cfg: {}, waiverMode: "rec", job: null,
  run: null, archived: null, archivedErr: null, compare: null, runs: [] };

// The snapshot the snapshot-based pages render: an archived run when one is picked, else the latest.
const shown = () => (S.run ? S.archived : S.summary);
const shownErr = () => (S.run ? S.archivedErr : S.summaryErr);
const runQ = () => (S.run ? `?run=${encodeURIComponent(S.run)}` : "");
const runAmp = () => (S.run ? `&run=${encodeURIComponent(S.run)}` : "");
const actualOf = (pid) => { const o = shown() && shown().outcome; return o ? o.actual[pid] : undefined; };
function actualCell(pid, adj) {
  const a = actualOf(pid);
  if (a === undefined) return "";
  if (a === null) return `<td class="num muted" title="Didn't play">DNP</td>`;
  const d = a - (adj || 0);
  return `<td class="num"><b>${f1(a)}</b> <span class="diff small ${dcls(d)}">${signed(d)}</span></td>`;
}
const noSnapshot = (what) => (shownErr() && shownErr().status !== 404 ? errorBanner(shownErr()) : S.run ? errorBanner(shownErr() || "Archived run not found") : emptyRun(what));

// ---- utils ---------------------------------------------------------------------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const f1 = (v) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(1));
const signed = (v, d = 1) => {
  if (v === null || v === undefined) return "–";
  const r = Number(v.toFixed(d));  // no sign on values that round to zero
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toFixed(d);
};
const dcls = (v) => (v == null ? "" : Math.round(v * 10) > 0 ? "up" : Math.round(v * 10) < 0 ? "down" : "");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error((body && body.detail) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const skeleton = (n = 6) => `<div class="card">${Array.from({ length: n }, (_, i) => `<div class="skeleton" style="width:${90 - (i % 3) * 15}%"></div>`).join("")}</div>`;
const errorBanner = (e) => `<div class="banner error">${esc(e.message || e)}</div>`;
const emptyRun = (what) => `<div class="card empty"><h2>No pipeline runs yet</h2>
  <p class="muted">${esc(what)} comes from the latest pipeline run. Start one to populate this page.</p>
  <button class="btn primary" data-action="run">Run pipeline</button></div>`;

// ---- shared player components --------------------------------------------------
const plink = (id, name) => `<a class="pname" href="#player/${encodeURIComponent(id)}${runQ()}">${esc(name)}</a>`;
const posTag = (p) => `<span class="pos">${esc(p)}</span>`;

function oppText(e) {
  if (e.on_bye) return "BYE";
  if (!e.opp) return "–";
  return (e.is_home ? "vs " : "@ ") + e.opp;
}

function injuryChip(status) {
  if (!status) return "";
  const cls = status === "Questionable" ? "warn" : ["Doubtful", "Out", "IR", "PUP", "Sus", "COV"].includes(status) ? "bad" : "";
  const short = { Questionable: "Q", Doubtful: "D", Out: "OUT", Suspended: "SUS" }[status] || status;
  return `<span class="chip ${cls}" title="${esc(status)}">${esc(short)}</span>`;
}

function ownerChip(owner) {
  if (owner === "Mine") return `<span class="chip info">Mine</span>`;
  if (owner) return `<span class="chip">${esc(owner)}</span>`;
  return `<span class="chip good">Free agent</span>`;
}

const gradePill = (g) => (g && g !== "-" ? `<span class="grade ${g}">${g}</span>` : `<span class="muted">–</span>`);

function sentimentBar(v, n) {
  v = v || 0;
  const w = Math.min(Math.abs(v), 1) * 35;
  const fill = v >= 0 ? `left:50%;width:${w}px` : `left:${35 - w}px;width:${w}px`;
  return `<span class="sbar" title="${n ?? 0} articles"><span class="track"><span class="fill ${v >= 0 ? "up" : "down"}" style="${fill}"></span></span>
    <span class="val">${signed(v, 2)}</span>${n !== undefined ? `<span class="muted small">(${n})</span>` : ""}</span>`;
}

const MULTS = [["mult_injury", "injury"], ["mult_matchup", "matchup"], ["mult_weather", "weather"], ["mult_sentiment", "news"]];

function multChips(e) {
  return `<span class="chips">${MULTS.filter(([k]) => e[k] !== undefined && Math.abs(e[k] - 1) > 0.0005)
    .map(([k, label]) => `<span class="chip ${e[k] > 1 ? "good" : "bad"}">×${e[k].toFixed(2)} ${label}</span>`).join("")}</span>`;
}

function whyPanel(e, weights) {
  const w = weights || { projection: 0.7, recent_form: 0.3 };
  const baseNote = e.proj != null && e.recent_avg != null
    ? `${w.projection}×${f1(e.proj)} + ${w.recent_form}×${f1(e.recent_avg)}`
    : e.proj != null ? "projection only" : e.recent_avg != null ? "recent form only" : "no data";
  const step = (k, v, note, cls = "") => `<div class="step ${cls}" title="${esc(note || "")}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const m = (k) => (e[k] > 1.0005 ? "up" : e[k] < 0.9995 ? "down" : "");
  const meta = [];
  if (e.def_rank) meta.push(`Opp defense vs ${esc(e.position)}: rank ${e.def_rank}/${e.def_teams} (${e.def_ratio}× avg allowed)`);
  if (e.implied != null) meta.push(`Implied team total: ${f1(e.implied)}`);
  if (e.weather) meta.push(`Weather: ${esc(e.weather)}`);
  if (e.n_articles) meta.push(`News: ${e.n_articles} articles, sentiment ${signed(e.sentiment, 2)}`);
  if (e.notes && e.notes.length) meta.push(`Notes: ${esc(e.notes.join(", "))}`);
  return `<div class="why">
      ${step("base", f1(e.base), baseNote)}
      <span class="op">×</span>${step("injury", e.mult_injury.toFixed(2), e.injury_status || "healthy", m("mult_injury"))}
      <span class="op">×</span>${step("matchup", e.mult_matchup.toFixed(2), "defense vs position + Vegas implied total", m("mult_matchup"))}
      <span class="op">×</span>${step("weather", e.mult_weather.toFixed(2), e.weather || "no forecast", m("mult_weather"))}
      <span class="op">×</span>${step("news", e.mult_sentiment.toFixed(2), "1 + weight × sentiment", m("mult_sentiment"))}
      <span class="op">=</span>${step("adj pts", f1(e.adj), "", "result")}
    </div>
    <div class="why-meta">${meta.map((x) => `<span>${x}</span>`).join("") || `<span class="muted">No matchup, weather or news signals.</span>`}</div>`;
}

function articleList(arts) {
  if (!arts || !arts.length) return `<p class="muted small">No articles in the lookback window.</p>`;
  return `<ul class="articles">${arts.map((a) => `<li>
      <span class="chip ${a.score > 0.05 ? "good" : a.score < -0.05 ? "bad" : ""}">${signed(a.score, 2)}</span>
      <span>${/^https?:\/\//i.test(a.link || "") ? `<a href="${esc(a.link)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title)}
      <span class="src">· ${esc(a.source || "")}${a.published ? " · " + ago(a.published) : ""}</span></span></li>`).join("")}</ul>`;
}

// ---- header / run job ----------------------------------------------------------
function renderHeader() {
  const s = S.summary;
  if (s) {
    $("#team-name").textContent = s.meta.team || "Fantasy Analyzer";
    $("#team-sub").textContent = `${s.meta.league || ""} · Week ${s.meta.week}, ${s.meta.season}`;
    const chip = $("#snap-age");
    chip.hidden = !!S.run;
    chip.className = "chip " + (s.stale ? "warn" : "");
    chip.textContent = (s.stale ? "Stale · " : "Updated ") + ago(s.meta.generated_at);
    chip.title = `Snapshot from ${new Date(s.meta.generated_at).toLocaleString()}` +
      (s.current_week && s.current_week !== s.meta.week ? ` (week ${s.meta.week}; current week is ${s.current_week})` : "");
  } else if (S.cfg.team_name) {
    $("#team-name").textContent = S.cfg.team_name;
  }
  renderPicker();
}

const runLabel = (r) => `${new Date(r.generated_at).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` +
  ` · Wk ${r.week} · ${f1(r.total)}${r.options && r.options.news === false ? " · no news" : ""}`;

function renderPicker() {
  const sel = $("#run-picker");
  const past = S.runs.filter((r) => !r.latest);
  sel.hidden = !past.length && !S.run;
  sel.innerHTML = `<option value="">Latest run</option>` +
    (past.length ? `<optgroup label="Archived runs">${past.map((r) => `<option value="${esc(r.id)}">${esc(runLabel(r))}</option>`).join("")}</optgroup>` : "");
  sel.value = S.run || "";
  sel.classList.toggle("archived", !!S.run);
}

async function loadRuns() {
  try { S.runs = await api("/api/runs"); } catch { S.runs = []; }
  renderPicker();
}

async function selectRun(run) {
  run = run || null;
  if (run === S.run) return;
  S.run = run;
  S.archived = S.archivedErr = S.compare = null;
  if (!run) return;
  try {
    [S.archived, S.compare] = await Promise.all([api(`/api/summary${runQ()}`), api(`/api/compare${runQ()}`).catch(() => null)]);
  } catch (e) { S.archivedErr = e; }
}

function archivedBanner() {
  const s = shown();
  if (!S.run || !s) return "";
  const done = s.outcome || (s.current_week != null && s.meta.week < s.current_week);
  return `<div class="banner info"><b>Viewing an archived run</b> from ${esc(new Date(s.meta.generated_at).toLocaleString())} (week ${s.meta.week}).
    ${done ? `Every page shows data only through the end of week ${s.meta.week}, with that week's actual results next to the predictions.`
           : `Week ${s.meta.week} isn't over yet, so there are no results to compare.`}
    <a href="#${currentTab()}">Back to latest →</a></div>`;
}

function outcomeCard() {
  const s = shown();
  if (!S.run || !s) return "";
  if (s.outcome_error) return `<div class="banner error">Couldn't load week ${s.meta.week} results: ${esc(s.outcome_error)}</div>`;
  const o = s.outcome;
  if (!o) return "";
  const L = o.lineup, eff = L.best ? Math.round((100 * L.recommended) / L.best) : null;
  const tile = (label, v, hint) => `<div class="tile"><div class="label">${label}</div><div class="value">${f1(v)}</div><div class="hint">${hint}</div></div>`;
  return `<div class="card"><h2>Prediction vs outcome <span class="muted small">week ${o.week}</span></h2>
    <div class="tiles" style="margin-bottom:8px">
      ${tile("Projected (recommended lineup)", L.projected, "what this run predicted")}
      ${tile("Actual (recommended lineup)", L.recommended, `<span class="diff ${dcls(L.recommended - L.projected)}">${signed(L.recommended - L.projected)}</span> vs projection`)}
      ${tile("Actual (lineup you played)", L.played, `recommendation scored <span class="diff ${dcls(L.recommended - L.played)}">${signed(L.recommended - L.played)}</span> vs this`)}
      ${tile("Best possible (hindsight)", L.best, eff != null ? `recommendation captured ${eff}%` : "")}
    </div>
    <p class="small muted">${o.errors.n ? `Across ${o.errors.n} rostered players who played: mean absolute error ${f1(o.errors.mae)} pts,
      bias ${signed(o.errors.bias)} (positive = scored more than predicted).` : "No rostered players had results."}
      The Actual column on Roster and Waivers shows each player's result.</p></div>`;
}

function compareCard() {
  const c = S.compare;
  if (!S.run || !c) return "";
  const changed = c.players.filter((p) => p.status !== "kept" || (p.delta && Math.abs(p.delta) >= 0.05) || p.slot_old !== p.slot_new);
  const unchanged = c.players.length - changed.length;
  const cell = (slot, adj) => (adj == null ? `<span class="muted">–</span>` : `<span class="muted small">${esc(slot || "")}</span> ${f1(adj)}`);
  const names = (list) => list.map((r) => plink(r.player_id, r.name)).join(", ") || `<span class="muted">none</span>`;
  return `<div class="card"><h2>Compared with latest</h2>
    <p>Lineup total <b>${f1(c.old.total)}</b> → <b>${f1(c.new.total)}</b>
      <span class="diff ${dcls(c.total_delta)}">(${signed(c.total_delta)})</span>
      ${c.same_week ? "" : `<span class="chip warn">different weeks</span> <span class="muted small">week ${c.old.week} vs week ${c.new.week}, so matchups differ</span>`}</p>
    ${changed.length ? `<div class="table-wrap"><table><thead><tr><th>Player</th><th>Pos</th><th class="num">Then</th><th class="num">Now</th><th class="num">Δ adj</th></tr></thead><tbody>
      ${changed.map((p) => `<tr><td>${plink(p.player_id, p.name)} ${p.status === "added" ? `<span class="chip good">added</span>` : p.status === "dropped" ? `<span class="chip bad">dropped</span>` : ""}
        ${p.slot_old && p.slot_new && p.slot_old !== p.slot_new ? `<span class="chip">${esc(p.slot_old)} → ${esc(p.slot_new)}</span>` : ""}</td>
        <td>${posTag(p.position)}</td><td class="num">${cell(p.slot_old, p.adj_old)}</td><td class="num">${cell(p.slot_new, p.adj_new)}</td>
        <td class="num">${p.delta == null ? "–" : `<span class="diff ${dcls(p.delta)}">${signed(p.delta)}</span>`}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted">No roster or lineup changes.</p>`}
    ${unchanged ? `<p class="small muted">${unchanged} player${unchanged > 1 ? "s" : ""} unchanged.</p>` : ""}
    <h3>Waiver targets</h3>
    <p class="small"><b>New:</b> ${names(c.recs_new)}<br><b>No longer recommended:</b> ${names(c.recs_gone)}<br><b>Still recommended:</b> ${names(c.recs_kept)}</p>
  </div>`;
}

function renderJob() {
  const j = S.job;
  const el = $("#run-status");
  const running = j && j.state === "running";
  $("#run-btn").disabled = running;
  $("#run-menu-btn").disabled = running;
  $("#run-btn").textContent = running ? "Running…" : "Run pipeline";
  if (!j || j.state === "idle") { el.hidden = true; return; }
  el.hidden = false;
  if (running) {
    const last = (j.log || []).filter((l) => l.trim()).slice(-1)[0] || "Starting…";
    el.innerHTML = `<span class="spinner"></span>${esc(last.replace(/^\s*-\s*/, ""))}`;
    el.title = (j.log || []).join("\n");
  } else if (j.state === "error") {
    el.innerHTML = `<span class="chip bad">Run failed</span> ${esc(j.error || "")}`;
    el.title = (j.log || []).join("\n");
  } else {
    el.hidden = true;
  }
}

let pollTimer = null;
async function pollJob() {
  clearTimeout(pollTimer);
  try { S.job = await api("/api/run"); } catch { return; }
  renderJob();
  if (S.job.state === "running") {
    pollTimer = setTimeout(pollJob, 1500);
  } else if (S.job.state === "done" && S.job.finished_at !== S.lastFinished) {
    S.lastFinished = S.job.finished_at;
    S.pool = null; S.news = null;
    await Promise.all([loadSummary(), loadRuns()]);
    if (S.run) { const r = S.run; S.run = null; await selectRun(r); }
    route();
  }
}

async function startRun() {
  $("#run-menu").hidden = true;
  try {
    S.job = await api("/api/run", { method: "POST", body: JSON.stringify({ news: $("#opt-news").checked, weather: $("#opt-weather").checked }) });
  } catch (e) {
    if (e.status !== 409) { alert(e.message); return; }
    S.job = e.body;
  }
  renderJob();
  pollJob();
}

async function loadSummary() {
  try { S.summary = await api("/api/summary"); S.summaryErr = null; }
  catch (e) { S.summary = null; S.summaryErr = e; }
  renderHeader();
}

// ---- Summary -------------------------------------------------------------------
function pageSummary() {
  const s = shown();
  if (!s) return noSnapshot("The summary");
  const P = s.players;
  const recs = s.waiver_recs;
  const top = recs[0] && P[recs[0].player_id];
  const nChanges = s.changes.start.length;
  const holes = s.lineup.filter((l) => !l.player_id || P[l.player_id].adj <= 0);

  const items = [];
  s.changes.start.forEach((id) => items.push(["START", "good", `${plink(id, P[id].name)} ${posTag(P[id].position)} <span class="muted">${f1(P[id].adj)} adj · ${esc(oppText(P[id]))}</span>`]));
  s.changes.bench.forEach((id) => items.push(["BENCH", "warn", `${plink(id, P[id].name)} ${posTag(P[id].position)} <span class="muted">${f1(P[id].adj)} adj${P[id].notes.length ? " · " + esc(P[id].notes.join(", ")) : ""}</span>`]));
  holes.forEach((l) => items.push(["HOLE", "bad", l.player_id
    ? `No healthy <b>${esc(l.slot)}</b>: ${plink(l.player_id, P[l.player_id].name)} (${esc(P[l.player_id].notes.join(", ") || "no projection")}). <a href="#waivers${runQ()}">Check waivers</a>`
    : `Nobody eligible for <b>${esc(l.slot)}</b>. <a href="#waivers${runQ()}">Check waivers</a>`]));
  const holeIds = new Set(holes.map((l) => l.player_id));
  s.alerts.filter((id) => !s.changes.bench.includes(id) && !holeIds.has(id)).forEach((id) =>
    items.push(["ALERT", "warn", `${plink(id, P[id].name)} ${posTag(P[id].position)} <span class="muted">${esc(P[id].notes.join(", "))}</span>`]));
  recs.slice(0, 3).forEach((r) => {
    const p = P[r.player_id];
    items.push(["ADD", "info", `${plink(r.player_id, p.name)} ${posTag(p.position)} <span class="muted">${signed(r.weekly_gain)} this week · ${signed(r.ros_gain)} vs drop</span>
      ${r.drop_id ? `· drop ${plink(r.drop_id, P[r.drop_id].name)}` : ""} · <b>${esc(r.bid)}</b>`]);
  });

  const lineupRows = s.lineup.map((l) => {
    const e = l.player_id && P[l.player_id];
    if (!e) return `<tr><td class="muted">${esc(l.slot)}</td><td colspan="4" class="muted">(empty)</td></tr>`;
    return `<tr><td class="muted">${esc(l.slot)}</td><td>${plink(e.player_id, e.name)} ${injuryChip(e.injury_status)}</td>
      <td class="muted nw">${esc(oppText(e))}</td><td class="hide-sm">${multChips(e)}</td><td class="adj">${f1(e.adj)}</td></tr>`;
  }).join("");

  return `${archivedBanner()}${outcomeCard()}${compareCard()}
    <div class="tiles">
      <div class="tile"><div class="label">Projected total (adjusted)</div><div class="value">${f1(s.total)}</div>
        <div class="hint">recommended lineup</div></div>
      <div class="tile"><div class="label">Lineup changes</div><div class="value">${nChanges}</div>
        <div class="hint">${nChanges ? "vs. your Sleeper lineup" : "Sleeper lineup matches"}</div></div>
      <div class="tile"><div class="label">Injury / bye alerts</div><div class="value">${s.alerts.length}</div>
        <div class="hint">${holes.length ? `${holes.length} empty slot${holes.length > 1 ? "s" : ""}` : "on active roster"}</div></div>
      <div class="tile"><div class="label">Top waiver target</div>
        ${top ? `<div class="value sm">${plink(recs[0].player_id, top.name)}</div><div class="hint">${signed(recs[0].weekly_gain)} pts this week · ${esc(recs[0].bid)}</div>`
              : `<div class="value sm muted">None</div><div class="hint">nobody clears the thresholds</div>`}</div>
    </div>
    <div class="grid2">
      <div class="card"><h2>Before kickoff</h2>
        ${items.length ? `<ul class="checklist">${items.map(([t, c, h]) => `<li><span class="tag"><span class="chip ${c}">${t}</span></span><span>${h}</span></li>`).join("")}</ul>`
                       : `<p class="muted">Nothing to do. Your lineup is set and there are no alerts.</p>`}
      </div>
      <div class="card"><h2>Recommended lineup</h2>
        <div class="table-wrap"><table><tbody>${lineupRows}</tbody>
        <tfoot><tr><td></td><td class="strong">Total</td><td></td><td class="hide-sm"></td><td class="adj">${f1(s.total)}</td></tr></tfoot></table></div>
        <p class="small muted" style="margin:10px 0 0"><a href="#roster${runQ()}">Full roster and breakdowns →</a></p>
      </div>
    </div>
    ${s.warnings.length ? `<div class="card"><details class="warnings"><summary>Run warnings (${s.warnings.length})</summary>
      <ul>${s.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details></div>` : ""}
    <p class="small muted">Run options: news ${s.meta.options.news === false ? "off" : "on"}, weather ${s.meta.options.weather === false ? "off" : "on"} ·
      generated ${esc(new Date(s.meta.generated_at).toLocaleString())}</p>`;
}

// ---- Roster --------------------------------------------------------------------
function rosterRow(e, flag, weights) {
  return `<tr class="clickable ${flag ? "flag-" + flag : ""}" data-expand="${esc(e.player_id)}">
    <td>${e._slot ? `<span class="muted small">${esc(e._slot)}</span> ` : ""}${plink(e.player_id, e.name)}
      ${flag === "start" ? `<span class="chip good">START</span>` : flag === "bench" ? `<span class="chip warn">BENCH</span>` : ""}</td>
    <td>${posTag(e.position)}</td><td class="muted">${esc(e.team || "FA")}</td><td class="nw">${esc(oppText(e))}</td>
    <td class="num">${f1(e.proj)}</td><td class="num hide-sm">${f1(e.recent_avg)}</td>
    <td>${gradePill(e.matchup_grade)}</td><td class="hide-sm small muted">${esc(e.weather || "–")}</td>
    <td class="hide-sm">${sentimentBar(e.sentiment, e.n_articles)}</td><td>${injuryChip(e.injury_status)}</td>
    <td class="adj">${f1(e.adj)}</td>${actualCell(e.player_id, e.adj)}</tr>
    <tr class="detail" data-detail="${esc(e.player_id)}" hidden><td colspan="12">${whyPanel(e, weights)}
      <a class="small" href="#player/${encodeURIComponent(e.player_id)}${runQ()}">Open player page →</a></td></tr>`;
}

const rosterHead = () => `<thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Opp</th><th class="num">Proj</th><th class="num hide-sm">Recent</th>
  <th>Matchup</th><th class="hide-sm">Weather</th><th class="hide-sm">Sentiment</th><th>Inj</th><th class="num">Adj</th>
  ${shown() && shown().outcome ? `<th class="num" title="Actual points that week, and the difference from Adj">Actual</th>` : ""}</tr></thead>`;

function pageRoster() {
  const s = shown();
  if (!s) return noSnapshot("Your roster view");
  const P = s.players, w = s.meta.weights;
  const current = new Set(s.current_starters);
  const starterIds = new Set(s.lineup.map((l) => l.player_id).filter(Boolean));
  const starters = s.lineup.filter((l) => l.player_id).map((l) => ({ ...P[l.player_id], _slot: l.slot }));
  const bench = Object.values(P).filter((e) => e.group === "roster" && !starterIds.has(e.player_id)).sort((a, b) => b.adj - a.adj);
  const reserve = Object.values(P).filter((e) => e.group === "reserve");
  const startRows = starters.map((e) => rosterRow(e, current.has(e.player_id) ? "" : "start", w)).join("");
  const benchRows = bench.map((e) => rosterRow({ ...e, slot: null }, current.has(e.player_id) ? "bench" : "", w)).join("");
  return `${archivedBanner()}
    <div class="card"><h2>Starters <span class="muted small">recommended · total ${f1(s.total)}</span></h2>
      <div class="table-wrap"><table>${rosterHead()}<tbody>${startRows}</tbody></table></div></div>
    <div class="card"><h2>Bench</h2>
      <div class="table-wrap"><table>${rosterHead()}<tbody>${benchRows || `<tr><td colspan="12" class="muted">Empty</td></tr>`}</tbody></table></div></div>
    ${reserve.length ? `<div class="card"><h2>IR / Taxi</h2><div class="table-wrap"><table>${rosterHead()}<tbody>
      ${reserve.map((e) => rosterRow({ ...e, slot: null }, "", w)).join("")}</tbody></table></div></div>` : ""}
    <p class="small muted"><span class="chip good">START</span> recommended but not in your Sleeper lineup ·
      <span class="chip warn">BENCH</span> in your Sleeper lineup but the model would bench. Click a row for the breakdown.</p>`;
}

function bindExpand(root) {
  root.addEventListener("click", (ev) => {
    if (ev.target.closest("a, button")) return;
    const row = ev.target.closest("tr[data-expand]");
    if (!row) return;
    const det = root.querySelector(`tr[data-detail="${CSS.escape(row.dataset.expand)}"]`);
    if (!det) return;
    det.hidden = !det.hidden;
    row.classList.toggle("expanded", !det.hidden);
  });
}

// ---- Player --------------------------------------------------------------------
function searchBox(placeholder, onPick) {
  const wrap = document.createElement("div");
  wrap.className = "search";
  wrap.innerHTML = `<input type="search" placeholder="${esc(placeholder)}" autocomplete="off" aria-label="${esc(placeholder)}"><div class="results" hidden></div>`;
  const input = $("input", wrap), box = $(".results", wrap);
  let timer = null, items = [], hl = -1, seq = 0;
  const paint = () => {
    box.hidden = !items.length;
    box.innerHTML = items.map((p, i) => `<a href="#" data-i="${i}" class="${i === hl ? "hl" : ""}">
      <span>${esc(p.name)} <span class="muted small">${esc(p.position)} · ${esc(p.team || "FA")}</span></span>${ownerChip(p.owner)}</a>`).join("");
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q) { items = []; paint(); return; }
    timer = setTimeout(async () => {
      const my = ++seq;
      try {
        const r = await api(`/api/players/search?q=${encodeURIComponent(q)}${runAmp()}`);
        if (my === seq) { items = r; hl = r.length ? 0 : -1; paint(); }
      } catch (e) { box.hidden = false; box.innerHTML = `<div class="banner error" style="margin:6px">${esc(e.message)}</div>`; }
    }, 200);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") { hl = Math.min(hl + 1, items.length - 1); paint(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { hl = Math.max(hl - 1, 0); paint(); ev.preventDefault(); }
    else if (ev.key === "Enter" && items[hl]) { onPick(items[hl]); items = []; paint(); }
    else if (ev.key === "Escape") { items = []; paint(); }
  });
  box.addEventListener("mousedown", (ev) => {
    const a = ev.target.closest("a[data-i]");
    if (a) { ev.preventDefault(); onPick(items[+a.dataset.i]); items = []; paint(); }
  });
  input.addEventListener("blur", () => setTimeout(() => { items = []; paint(); }, 150));
  return wrap;
}

function niceMax(v) {
  if (v <= 5) return 5;
  const step = v <= 20 ? 5 : v <= 40 ? 10 : 20;
  return Math.ceil(v / step) * step;
}

function historyChart(h) {
  const W = 720, H = 240, L = 34, R = 8, T = 10, B = 28;
  const vals = h.flatMap((d) => [d.actual, d.projected]).filter((v) => v != null);
  const max = niceMax(Math.max(5, ...vals));
  const n = h.length, band = (W - L - R) / n, bw = Math.min(28, band * 0.55);
  const x = (i) => L + band * i + band / 2;
  const y = (v) => T + (H - T - B) * (1 - Math.max(0, v) / max);
  const base = y(0);
  const div = max % 4 === 0 ? 4 : 5;  // keep tick labels whole numbers
  const ticks = Array.from({ length: div + 1 }, (_, i) => (max * i) / div);
  const bar = (cx, v, cls) => {
    const top = y(v), r = Math.min(4, (base - top) / 2), x0 = cx - bw / 2, x1 = cx + bw / 2;
    if (base - top < 0.5) return "";
    return `<path class="${cls}" d="M${x0},${base} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x1 - r} Q${x1},${top} ${x1},${top + r} V${base} Z"/>`;
  };
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Weekly fantasy points: actual vs projected">`;
  ticks.forEach((t) => {
    svg += `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)" stroke-width="1"/>
      <text x="${L - 6}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${Math.round(t)}</text>`;
  });
  h.forEach((d, i) => {
    if (d.current && d.actual == null && d.projected != null) {
      const top = y(d.projected);
      svg += `<rect x="${x(i) - bw / 2}" y="${top}" width="${bw}" height="${Math.max(0, base - top)}" rx="3" fill="none" stroke="var(--series-1)" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    } else if (d.actual != null) {
      svg += bar(x(i), d.actual, "").replace("<path", `<path fill="var(--series-1)"`);
    }
    const lab = d.bye ? "BYE" : `W${d.week}`;
    svg += `<text x="${x(i)}" y="${H - 10}" text-anchor="middle" font-size="11" fill="${d.current ? "var(--text)" : "var(--muted)"}" font-weight="${d.current ? 700 : 400}">${lab}</text>`;
  });
  const pts = h.map((d, i) => (d.projected != null && !d.bye ? [x(i), y(d.projected)] : null));
  let path = "", pen = false;
  pts.forEach((p) => { if (p) { path += (pen ? "L" : "M") + p[0] + "," + p[1]; pen = true; } else pen = false; });
  svg += `<path d="${path}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round"/>`;
  pts.forEach((p) => { if (p) svg += `<circle cx="${p[0]}" cy="${p[1]}" r="4" fill="var(--series-2)" stroke="var(--surface)" stroke-width="2"/>`; });
  h.forEach((d, i) => { svg += `<rect class="hit" data-i="${i}" x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}" fill="transparent"/>`; });
  svg += `<line x1="${L}" x2="${W - R}" y1="${base}" y2="${base}" stroke="var(--border)" stroke-width="1"/></svg>`;
  return `<div class="legend"><span><span class="sw" style="background:var(--series-1)"></span>Actual</span>
    <span><span class="sw line" style="background:var(--series-2)"></span>Projected</span>
    ${h.some((d) => d.current && d.actual == null && d.projected != null) ? `<span><span class="sw outline"></span>This week (projected)</span>` : ""}</div><div class="chart">${svg}</div>`;
}

function bindChart(root, h) {
  const tip = $("#tooltip");
  root.querySelectorAll(".chart .hit").forEach((r) => {
    r.addEventListener("mousemove", (ev) => {
      const d = h[+r.dataset.i];
      const diff = d.actual != null && d.projected != null ? d.actual - d.projected : null;
      tip.innerHTML = `<div class="tt-h">Week ${d.week} ${d.opp ? "· " + esc(d.opp) : ""}</div>
        ${d.bye ? `<div class="muted">Bye week</div>` : `
        <div class="tt-r"><span>Actual</span><b>${d.current && d.actual == null ? "–" : d.actual == null ? "DNP" : f1(d.actual)}</b></div>
        <div class="tt-r"><span>Projected</span><b>${f1(d.projected)}</b></div>
        ${diff != null ? `<div class="tt-r"><span>Diff</span><b class="diff ${diff >= 0 ? "up" : "down"}">${signed(diff)}</b></div>` : ""}`}`;
      tip.hidden = false;
      const x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
      tip.style.left = x + "px";
      tip.style.top = ev.clientY + 14 + "px";
    });
    r.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

const STAT_LABELS = { pass_yd: "Pass yd", pass_td: "Pass TD", pass_int: "INT", rush_att: "Att", rush_yd: "Rush yd", rush_td: "Rush TD",
  rec_tgt: "Tgt", rec: "Rec", rec_yd: "Rec yd", rec_td: "Rec TD", fgm: "FGM", fga: "FGA", xpm: "XPM" };

const statFmt = (v) => (v == null ? "–" : Number.isInteger(v) ? v : Number(v).toFixed(1));

async function pagePlayer(pid) {
  const top = document.createElement("div");
  top.appendChild(searchBox("Search any NFL player…", (p) => { location.hash = `#player/${p.player_id}`; }));
  const body = document.createElement("div");
  view.replaceChildren(top, body);
  if (!pid) {
    const s = S.summary;
    const mine = s ? Object.values(s.players).filter((e) => e.group === "roster").sort((a, b) => b.adj - a.adj) : [];
    body.innerHTML = mine.length ? `<h3>Your roster</h3><div class="picks">${mine.map((e) =>
      `<a class="fchip" href="#player/${encodeURIComponent(e.player_id)}">${esc(e.name)} <span class="muted">${esc(e.position)}</span></a>`).join("")}</div>`
      : `<p class="muted">Search for a player to see past performance and projections.</p>`;
    $("input", top).focus();
    return;
  }
  body.innerHTML = skeleton(8);
  let d;
  try { d = await api(`/api/players/${encodeURIComponent(pid)}${runQ()}`); }
  catch (e) { body.innerHTML = errorBanner(e); return; }
  const e = d.eval, weights = S.summary && S.summary.meta.weights;
  const statKeys = Object.keys(STAT_LABELS).filter((k) => d.history.some((x) => x.stats[k]));
  const log = d.history.filter((x) => !x.current || x.actual != null).map((x) => {
    const diff = x.actual != null && x.projected != null ? x.actual - x.projected : null;
    return `<tr><td>W${x.week}</td><td class="muted">${esc(x.opp || "–")}</td>
      <td class="num strong">${x.bye ? "BYE" : x.actual == null ? "DNP" : f1(x.actual)}</td><td class="num">${f1(x.projected)}</td>
      <td class="num">${diff == null ? "–" : `<span class="diff ${diff >= 0 ? "up" : "down"}">${signed(diff)}</span>`}</td>
      ${statKeys.map((k) => `<td class="num hide-sm">${statFmt(x.stats[k])}</td>`).join("")}</tr>`;
  }).reverse().join("");
  const result = d.as_of ? (d.actual == null ? `<span class="chip">didn't play</span>`
    : `<span class="chip ${dcls(d.actual - e.adj) === "up" ? "good" : dcls(d.actual - e.adj) === "down" ? "bad" : ""}">actual ${f1(d.actual)} (${signed(d.actual - e.adj)})</span>`) : "";
  body.innerHTML = `${archivedBanner()}
    <div class="phead">
      <div><div class="name">${esc(d.name)}</div>
        <div class="meta">${posTag(d.position)} ${esc(d.team || "FA")}${d.number ? " · #" + esc(d.number) : ""}${d.age ? " · age " + esc(d.age) : ""}
          ${injuryChip(d.injury_status)}${d.injury_detail ? `<span class="small">${esc(d.injury_detail)}</span>` : ""} ${ownerChip(d.owner)}
          ${d.trending_adds ? `<span class="chip info">+${d.trending_adds.toLocaleString()} adds (48h)</span>` : ""}</div></div>
      <div class="stat-row">
        <div><div class="k">Week ${d.week} adj</div><div class="v">${f1(e.adj)}</div></div>
        ${d.as_of ? `<div><div class="k">Week ${d.week} actual</div><div class="v">${d.actual == null ? "–" : f1(d.actual)}</div></div>` : ""}
        <div><div class="k">${d.as_of ? `Pts wk 1–${d.week - 1}` : "Season pts"}</div><div class="v">${f1(d.season_pts)}</div></div>
        <div><div class="k">Pts / game</div><div class="v">${d.games ? f1(d.season_pts / d.games) : "–"}</div></div>
      </div>
    </div>
    <div class="card"><h2>Weekly points <span class="muted small">${d.season} season${d.as_of ? ` · through week ${d.week}` : ""}</span></h2>${historyChart(d.history)}</div>
    <div class="card"><h2>${d.as_of ? `Week ${d.week} prediction` : "This week"} · ${esc(oppText(e))} ${result} ${e.matchup_grade && e.matchup_grade !== "-" ? gradePill(e.matchup_grade) : ""}
        ${d.eval_source === "live" ? `<span class="chip warn" title="Not in the latest pipeline run, so news sentiment isn't included">live estimate · no news</span>` : ""}</h2>
      ${whyPanel(e, weights)}</div>
    <div class="card"><h2>Game log</h2><div class="table-wrap"><table>
      <thead><tr><th>Wk</th><th>Opp</th><th class="num">Actual</th><th class="num">Proj</th><th class="num">Diff</th>
        ${statKeys.map((k) => `<th class="num hide-sm">${STAT_LABELS[k]}</th>`).join("")}</tr></thead>
      <tbody>${log || `<tr><td colspan="5" class="muted">No games yet this season.</td></tr>`}</tbody></table></div></div>
    <div class="card"><h2>News <span class="muted small">${e.n_articles ? `sentiment ${signed(e.sentiment, 2)} · ×${e.mult_sentiment.toFixed(2)}` : ""}</span></h2>
      <div id="pnews">${e.articles && e.articles.length ? articleList(e.articles) : `<p class="muted small">Not scored in ${S.run ? "this" : "the latest"} run.</p>`}</div>
      ${S.run ? `<p class="small muted">Live news lookup is off while viewing an archived run, because it would show today's articles.</p>`
              : `<button class="btn sm" id="fetch-news">Fetch latest news</button>`}</div>`;
  bindChart(body, d.history);
  if (!S.run) $("#fetch-news", body).addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true; btn.textContent = "Fetching…";
    try {
      const n = await api(`/api/news/${encodeURIComponent(pid)}`);
      $("#pnews", body).innerHTML = `<p class="small">${sentimentBar(n.sentiment, n.n_articles)} → ×${n.mult_sentiment.toFixed(2)}</p>${articleList(n.articles)}`;
      btn.textContent = "Refresh";
    } catch (err) { $("#pnews", body).innerHTML = errorBanner(err); btn.textContent = "Retry"; }
    btn.disabled = false;
  });
}

// ---- Waivers -------------------------------------------------------------------
const W = { q: "", pos: new Set(), team: "", hideInj: false, trending: false, sort: "adj", dir: -1, page: 0 };
const BROWSE_COLS = [["proj", "Proj"], ["recent_avg", "Recent"], ["adj", "Adj"], ["season_pts", "Season"], ["ppg", "Pts/g"], ["trending_adds", "Adds 48h"]];

function pageWaivers() {
  const wrap = document.createElement("div");
  wrap.innerHTML = `<div class="toolbar"><div class="seg" role="tablist">
      <button data-mode="rec" class="${S.waiverMode === "rec" ? "on" : ""}">Recommended</button>
      <button data-mode="browse" class="${S.waiverMode === "browse" ? "on" : ""}">Browse all</button></div></div><div id="wbody"></div>`;
  view.replaceChildren(wrap);
  wrap.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => { S.waiverMode = b.dataset.mode; pageWaivers(); }));
  const body = $("#wbody", wrap);
  if (S.waiverMode === "rec") body.innerHTML = waiverRecs();
  else browse(body);
}

function waiverRecs() {
  const s = shown();
  if (!s) return noSnapshot("Waiver recommendations");
  const P = s.players;
  if (!s.waiver_recs.length) return `<div class="card empty"><h2>No free agents clear the thresholds</h2>
    <p class="muted">Try <b>Browse all</b> to look through the full pool and run what-ifs.</p></div>`;
  const rows = s.waiver_recs.map((r, i) => {
    const e = P[r.player_id], drop = r.drop_id && P[r.drop_id];
    return `<tr class="clickable" data-expand="${esc(r.player_id)}"><td class="muted">${i + 1}</td>
      <td>${plink(r.player_id, e.name)} ${injuryChip(e.injury_status)}</td><td>${posTag(e.position)}</td><td class="muted">${esc(e.team)}</td>
      <td>${esc(oppText(e))}</td><td class="adj">${f1(e.adj)}</td><td class="num hide-sm">${f1(e.recent_avg)}</td>
      <td class="num"><span class="diff ${r.weekly_gain > 0 ? "up" : ""}">${signed(r.weekly_gain)}</span></td>
      <td class="num"><span class="diff ${r.ros_gain > 0 ? "up" : r.ros_gain < 0 ? "down" : ""}">${signed(r.ros_gain)}</span></td>
      <td class="num hide-sm">${e.trending_adds ? e.trending_adds.toLocaleString() : "–"}</td>
      <td>${drop ? plink(r.drop_id, drop.name) : "–"}</td><td class="strong">${esc(r.bid)}</td>${actualCell(r.player_id, e.adj)}</tr>
      <tr class="detail" data-detail="${esc(r.player_id)}" hidden><td colspan="13">${whyPanel(e, s.meta.weights)}</td></tr>`;
  }).join("");
  const budget = s.meta.faab ? ` · FAAB left $${s.meta.budget_left}` : "";
  return `${archivedBanner()}<div class="card"><h2>Recommended adds <span class="muted small">week ${s.meta.week}${budget}</span></h2>
    <div class="table-wrap" id="recs"><table><thead><tr><th>#</th><th>Player</th><th>Pos</th><th>Team</th><th>Opp</th><th class="num">Adj</th>
      <th class="num hide-sm">Recent</th><th class="num" title="Change in this week's optimal lineup total">Gain (wk)</th>
      <th class="num" title="Avg of projection and recent form vs. the suggested drop">Vs drop</th><th class="num hide-sm">Adds 48h</th>
      <th>Drop</th><th>Bid</th>${s.outcome ? `<th class="num" title="Actual points that week, and the difference from Adj">Actual</th>` : ""}</tr></thead><tbody>${rows}</tbody></table></div>
    <p class="small muted">Gain (wk) = change in this week's optimal lineup total. Vs drop compares the average of projection and recent form with the suggested drop.</p></div>`;
}

async function browse(body) {
  if (!S.pool || S.pool.run !== S.run) {
    body.innerHTML = skeleton(10);
    try { S.pool = await api("/api/waivers/pool" + runQ()); }
    catch (e) { body.innerHTML = errorBanner(e); return; }
  }
  const teams = [...new Set(S.pool.players.map((p) => p.team).filter(Boolean))].sort();
  body.innerHTML = `${archivedBanner()}<div class="toolbar">
      <input class="input" type="search" id="wq" placeholder="Search name…" value="${esc(W.q)}" style="width:200px">
      ${["QB", "RB", "WR", "TE", "K", "DEF"].map((p) => `<button class="fchip ${W.pos.has(p) ? "on" : ""}" data-pos="${p}">${p}</button>`).join("")}
      <select id="wteam"><option value="">All teams</option>${teams.map((t) => `<option ${t === W.team ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
      <label><input type="checkbox" id="winj" ${W.hideInj ? "checked" : ""}> Hide injured</label>
      ${S.pool.as_of ? "" : `<label><input type="checkbox" id="wtrend" ${W.trending ? "checked" : ""}> Trending only</label>`}
    </div><div class="card" id="wtable"></div>`;
  const draw = () => drawPool($("#wtable", body));
  let t = null;
  $("#wq", body).addEventListener("input", (ev) => { clearTimeout(t); t = setTimeout(() => { W.q = ev.target.value; W.page = 0; draw(); }, 150); });
  body.querySelectorAll("[data-pos]").forEach((b) => b.addEventListener("click", () => {
    W.pos.has(b.dataset.pos) ? W.pos.delete(b.dataset.pos) : W.pos.add(b.dataset.pos);
    b.classList.toggle("on"); W.page = 0; draw();
  }));
  $("#wteam", body).addEventListener("change", (ev) => { W.team = ev.target.value; W.page = 0; draw(); });
  $("#winj", body).addEventListener("change", (ev) => { W.hideInj = ev.target.checked; W.page = 0; draw(); });
  if ($("#wtrend", body)) $("#wtrend", body).addEventListener("change", (ev) => { W.trending = ev.target.checked; W.page = 0; draw(); });
  draw();
}

function drawPool(el) {
  const size = (S.cfg.ui && S.cfg.ui.pool_page_size) || 50;
  const q = W.q.trim().toLowerCase();
  let rows = S.pool.players.filter((p) => (!q || p.name.toLowerCase().includes(q)) && (!W.pos.size || W.pos.has(p.position))
    && (!W.team || p.team === W.team) && (!W.hideInj || !p.injury_status) && (S.pool.as_of || !W.trending || p.trending_adds > 0));
  rows.sort((a, b) => ((a[W.sort] ?? -1e9) - (b[W.sort] ?? -1e9)) * W.dir);
  const pages = Math.max(1, Math.ceil(rows.length / size));
  W.page = Math.min(W.page, pages - 1);
  const slice = rows.slice(W.page * size, (W.page + 1) * size);
  const th = ([k, label]) => `<th class="num sortable ${W.sort === k ? "sorted" : ""} ${["recent_avg", "ppg", "trending_adds"].includes(k) ? "hide-sm" : ""}" data-sort="${k}">${label}${W.sort === k ? (W.dir < 0 ? " ↓" : " ↑") : ""}</th>`;
  const cell = (p, k) => {
    const v = p[k], cls = ["recent_avg", "ppg", "trending_adds"].includes(k) ? "hide-sm" : "";
    if (k === "adj") return `<td class="adj">${f1(v)}</td>`;
    if (k === "actual") return `<td class="num"><b>${v == null ? "–" : f1(v)}</b></td>`;
    if (k === "trending_adds") return `<td class="num ${cls}">${v ? v.toLocaleString() : "–"}</td>`;
    return `<td class="num ${cls}">${f1(v)}</td>`;
  };
  const P = S.pool, cols = P.as_of
    ? BROWSE_COLS.map(([k, l]) => (k === "season_pts" ? [k, `Wk 1–${P.week - 1}`] : k === "trending_adds" ? null : [k, l])).filter(Boolean)
      .concat([["actual", `Wk ${P.week} actual`]])
    : BROWSE_COLS;
  el.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Opp</th><th>Matchup</th>
      ${cols.map(th).join("")}<th></th></tr></thead><tbody>
      ${slice.map((p) => `<tr><td>${plink(p.player_id, p.name)} ${injuryChip(p.injury_status)} ${p.recommended ? `<span class="chip info">rec</span>` : ""}</td>
        <td>${posTag(p.position)}</td><td class="muted">${esc(p.team)}</td><td>${esc(oppText(p))}</td><td>${gradePill(p.matchup_grade)}</td>
        ${cols.map(([k]) => cell(p, k)).join("")}
        <td class="whatif" data-wi="${esc(p.player_id)}"><button class="btn sm" data-whatif="${esc(p.player_id)}">What if?</button></td></tr>`).join("")
        || `<tr><td colspan="12" class="muted">No players match these filters.</td></tr>`}
    </tbody></table></div>
    <div class="pager"><span>${rows.length.toLocaleString()} players · ${P.as_of
      ? `as of week ${P.week}: that week's free agents, projections and matchups (no news, injury or trending data)`
      : "live estimates (no news sentiment)"}</span>
      <span><button class="btn sm" data-page="-1" ${W.page === 0 ? "disabled" : ""}>‹ Prev</button>
      Page ${W.page + 1} / ${pages}
      <button class="btn sm" data-page="1" ${W.page >= pages - 1 ? "disabled" : ""}>Next ›</button></span></div>`;
  el.querySelectorAll("th[data-sort]").forEach((h) => h.addEventListener("click", () => {
    const k = h.dataset.sort;
    if (W.sort === k) W.dir *= -1; else { W.sort = k; W.dir = -1; }
    drawPool(el);
  }));
  el.querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => { W.page += +b.dataset.page; drawPool(el); }));
  el.querySelectorAll("[data-whatif]").forEach((b) => b.addEventListener("click", async () => {
    const td = b.parentElement;
    td.innerHTML = `<span class="muted">…</span>`;
    try {
      const r = await api(`/api/waivers/whatif/${encodeURIComponent(b.dataset.whatif)}${runQ()}`);
      td.innerHTML = `<span class="diff ${r.weekly_gain > 0 ? "up" : ""}" title="Lineup gain this week">${signed(r.weekly_gain)} wk</span> ·
        <span class="diff ${r.ros_gain > 0 ? "up" : r.ros_gain < 0 ? "down" : ""}" title="Value vs drop">${signed(r.ros_gain)} vs</span>
        ${r.drop ? `${plink(r.drop.player_id, r.drop.name)}` : ""} · <b>${esc(r.bid)}</b>`;
    } catch (e) { td.innerHTML = `<span class="diff down">${esc(e.message)}</span>`; }
  }));
}

// ---- News ----------------------------------------------------------------------
const N = { filter: "roster", sort: "signal" };

async function pageNews() {
  const wrap = document.createElement("div");
  view.replaceChildren(wrap);
  if (!S.news || S.news.run !== S.run) {
    wrap.innerHTML = skeleton(8);
    try { S.news = { ...(await api("/api/news" + runQ())), run: S.run }; }
    catch (e) { wrap.innerHTML = e.status === 404 ? emptyRun("News sentiment") : errorBanner(e); return; }
  }
  const n = S.news;
  wrap.innerHTML = `${archivedBanner()}${n.news_enabled === false ? `<div class="banner warn">${S.run ? "This archived run" : "The latest run"} skipped news. Run the pipeline with <b>News &amp; sentiment</b> checked to score articles.</div>` : ""}
    <div class="toolbar">
      ${[["roster", "My roster"], ["targets", "Waiver targets"], ["all", "All scored"]].map(([k, l]) => `<button class="fchip ${N.filter === k ? "on" : ""}" data-f="${k}">${l}</button>`).join("")}
      <span style="flex:1"></span>
      <div class="seg"><button data-s="signal" class="${N.sort === "signal" ? "on" : ""}">Strongest signal</button><button data-s="count" class="${N.sort === "count" ? "on" : ""}">Most articles</button></div>
    </div>
    <div class="card" id="nlist"></div>
    ${S.run ? "" : `<div class="card"><h2>Look up any player</h2><p class="muted small">Fetches and scores the latest articles live (Google News + NFL feeds).</p>
      <div id="nsearch"></div><div id="nlive" style="margin-top:12px"></div></div>`}
    <p class="small muted">Sentiment ranges from −1 to +1, weighted toward recent articles and pulled toward 0 when there are only a few. It enters the model as
      multiplier = 1 + ${n.sentiment_weight ?? 0.08} × sentiment.</p>`;
  wrap.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { N.filter = b.dataset.f; pageNews(); }));
  wrap.querySelectorAll("[data-s]").forEach((b) => b.addEventListener("click", () => { N.sort = b.dataset.s; pageNews(); }));
  drawNews($("#nlist", wrap));
  if (!S.run) $("#nsearch", wrap).appendChild(searchBox("Player name…", async (p) => {
    const out = $("#nlive", wrap);
    out.innerHTML = skeleton(3);
    try {
      const r = await api(`/api/news/${encodeURIComponent(p.player_id)}`);
      out.innerHTML = `<div class="strong">${plink(r.player_id, r.name)} <span class="muted small">${esc(r.position)} · ${esc(r.team || "FA")}</span></div>
        <p class="small">${sentimentBar(r.sentiment, r.n_articles)} → ×${r.mult_sentiment.toFixed(2)}</p>${articleList(r.articles)}`;
    } catch (e) { out.innerHTML = errorBanner(e); }
  }));
}

function drawNews(el) {
  let rows = S.news.players.filter((p) => N.filter === "all" ? p.n_articles > 0 || p.group === "roster"
    : N.filter === "roster" ? p.group === "roster" : p.group === "candidate" && p.n_articles > 0);
  rows = rows.slice().sort((a, b) => N.sort === "count" ? b.n_articles - a.n_articles : Math.abs(b.sentiment) - Math.abs(a.sentiment));
  el.innerHTML = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Player</th><th>Pos</th><th>Team</th><th>Sentiment</th>
      <th class="num">Multiplier</th><th></th></tr></thead><tbody>
    ${rows.map((p) => `<tr class="clickable" data-expand="${esc(p.player_id)}"><td>${plink(p.player_id, p.name)}
        ${p.group === "candidate" ? `<span class="chip ${p.recommended ? "info" : ""}">${p.recommended ? "rec add" : "FA"}</span>` : ""}</td>
      <td>${posTag(p.position)}</td><td class="muted">${esc(p.team || "")}</td><td>${sentimentBar(p.sentiment, p.n_articles)}</td>
      <td class="num">×${(p.mult_sentiment ?? 1).toFixed(2)}</td><td class="muted small">${p.n_articles ? "▾ articles" : ""}</td></tr>
      <tr class="detail" data-detail="${esc(p.player_id)}" hidden><td colspan="6">${articleList(p.articles)}</td></tr>`).join("")}
    </tbody></table></div>` : `<p class="muted">No scored players in this group.</p>`;
}

// ---- router --------------------------------------------------------------------
function parseHash() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const [tab, arg] = (path || "summary").split("/");
  return { tab: ["summary", "roster", "player", "waivers", "news"].includes(tab) ? tab : "summary", arg,
           run: new URLSearchParams(query || "").get("run") };
}
const currentTab = () => parseHash().tab;

async function route() {
  const { tab: name, arg, run } = parseHash();
  if ((run || null) !== S.run) {
    view.innerHTML = skeleton(8);
    await selectRun(run);
    renderHeader();
  }
  document.querySelectorAll("#tabs a").forEach((a) => {
    a.classList.toggle("active", a.dataset.tab === name);
    a.setAttribute("href", `#${a.dataset.tab}${runQ()}`);
  });
  $("#tooltip").hidden = true;
  if (name === "player") return pagePlayer(arg && decodeURIComponent(arg));
  if (name === "waivers") return pageWaivers();
  if (name === "news") return pageNews();
  view.innerHTML = name === "roster" ? pageRoster() : pageSummary();
}

document.addEventListener("click", (ev) => {
  if (ev.target.closest("[data-action=run]")) startRun();
  const menu = $("#run-menu");
  if (ev.target.closest("#run-menu-btn")) menu.hidden = !menu.hidden;
  else if (!ev.target.closest("#run-menu")) menu.hidden = true;
});
$("#run-btn").addEventListener("click", startRun);
$("#run-picker").addEventListener("change", (ev) => {
  const v = ev.target.value;
  location.hash = `#${currentTab() === "player" ? "summary" : currentTab()}${v ? `?run=${encodeURIComponent(v)}` : ""}`;
});
bindExpand(view);
window.addEventListener("hashchange", route);

(async function init() {
  view.innerHTML = skeleton(8);
  try { S.cfg = await api("/api/config"); } catch { /* optional */ }
  await Promise.all([loadSummary(), loadRuns()]);
  route();
  try {
    S.job = await api("/api/run");
    S.lastFinished = S.job.finished_at;
    renderJob();
    if (S.job.state === "running") pollJob();
  } catch { /* server restarting */ }
})();
