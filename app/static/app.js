/* NetMon Frontend — charts + sortable process list, 1s refresh */
"use strict";

const state = {
  sortKey: "name", sortDir: 1, servers: [], charts: {},
  ifaceSort: {}, lastIfaces: null, lastTable: [], visible: {},
  diskSortKey: "total", diskSortDir: -1, lastDisk: [],
  sysSortKey: "cpu", sysSortDir: -1, lastSys: [], lastHostSys: {},
  cpuMode: "live",  // "live" (EMA) | "avg10" (10 s rolling average)
  diskMode: "live",  // dito fuer Disk I/O
  lastServers: [],
  latencyCharts: {}, lastLatency: {},
  equalScale: false, lastSeries: null,
  lastStorageLoad: 0,
  detailProcs: {}, detailCharts: {},   // server -> {proc, chart}
  procHistory: {},                     // server -> [{ts, procs:[[name,cont,rx,tx],...]}]
  tipEl: null,
};

const COLORS = { rx: "#22d3ee", tx: "#f59e0b" };

function fmt(bps) {
  if (!isFinite(bps)) return "–";
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + " GB/s";
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + " MB/s";
  if (bps >= 1e3) return (bps / 1e3).toFixed(0) + " KB/s";
  return bps.toFixed(0) + " B/s";
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("de-DE", { hour12: false });
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- Charts aufbauen (einmalig pro Server) ---------- */

/* Eingefrorener HTML-Tooltip: wird NUR bei Mausbewegung aktualisiert und
 * zeigt exakt die Daten (in/out + Prozesse) des Hover-Zeitpunkts. Scrollt
 * der Chart weiter, bleibt er unveraendert stehen und folgt erst wieder
 * der Maus, wenn sie sich bewegt. */
let frozenTip = null; // {el, server}

function ensureTipEl() {
  if (state.tipEl) return state.tipEl;
  const el = document.createElement("div");
  el.id = "proctip";
  el.style.cssText =
    "position:fixed;z-index:9999;pointer-events:none;display:none;" +
    "background:rgba(15,23,42,.95);border:1px solid rgba(148,163,184,.3);" +
    "border-radius:8px;padding:8px 10px;font:12px/1.45 ui-monospace,monospace;" +
    "color:#e2e8f0;box-shadow:0 8px 24px rgba(0,0,0,.45);max-width:420px;white-space:nowrap";
  document.body.appendChild(el);
  state.tipEl = el;
  return el;
}

function hideTip() {
  if (state.tipEl) state.tipEl.style.display = "none";
  frozenTip = null;
}

function showTip(cx, cy, label, rx, tx, snap) {
  const el = ensureTipEl();
  let html =
    `<div style="color:#94a3b8;margin-bottom:4px">${esc(label)}</div>` +
    `<div><span style="color:${COLORS.rx}">▼ in ${fmt(rx)}</span>` +
    `&nbsp;&nbsp;<span style="color:${COLORS.tx}">▲ out ${fmt(tx)}</span></div>`;
  const procs = (snap && snap.procs || [])
    .filter(p => p[2] > 0 || p[3] > 0)
    .sort((a, b) => (b[2] + b[3]) - (a[2] + a[3]))
    .slice(0, 6);
  if (procs.length) {
    html += `<div style="border-top:1px solid rgba(148,163,184,.25);margin:6px 0 4px;padding-top:4px;color:#94a3b8">Top Prozesse</div>`;
    html += procs.map(p => {
      const nm = p[1]
        ? `${esc(p[0].length > 24 ? p[0].slice(0, 24) + "…" : p[0])} <span style="color:#22d3ee">[${esc(p[1])}]</span>`
        : esc(p[0].length > 30 ? p[0].slice(0, 30) + "…" : p[0]);
      return `<div style="display:flex;justify-content:space-between;gap:16px"><span>${nm}</span>` +
        `<span style="color:#94a3b8">▼ ${fmt(p[2])} ▲ ${fmt(p[3])}</span></div>`;
    }).join("");
  }
  el.innerHTML = html;
  el.style.display = "block";
  const w = el.offsetWidth, h = el.offsetHeight;
  let left = cx + 14, top = cy + 14;
  if (left + w > window.innerWidth - 8) left = cx - w - 14;
  if (top + h > window.innerHeight - 8) top = cy - h - 14;
  el.style.left = left + "px";
  el.style.top = top + "px";
}

/* Prozess-Snapshot des Servers zum Zeitpunkt ts (naechstliegender, aelterer Eintrag) */
function findSnap(serverName, ts) {
  const arr = state.procHistory[serverName];
  if (!arr || !arr.length) return null;
  if (ts == null) return arr[arr.length - 1];
  let lo = 0, hi = arr.length - 1, best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid].ts <= ts) { best = arr[mid]; lo = mid + 1; }
    else hi = mid - 1;
  }
  return best;
}

/* Datenindex zur Canvas-X-Position (eigenes Hit-Testing, robust gegen
 * Chart.js-Interaktions-Quirks) */
function chartIndexAt(ch, x) {
  const xScale = ch.scales && ch.scales.x;
  if (!xScale || typeof xScale.getPixelForValue !== "function") return null;
  const labels = ch.data.labels;
  if (!labels || !labels.length) return null;
  let best = null, bestDist = Infinity;
  for (let i = 0; i < labels.length; i++) {
    const d = Math.abs(xScale.getPixelForValue(i) - x);
    if (d < bestDist) { bestDist = d; best = i; }
  }
  return bestDist < 1e6 ? best : null;
}

function buildCharts(servers) {
  const grid = document.getElementById("chartgrid");
  grid.innerHTML = "";
  state.charts = {};
  state.servers = servers.map(s => s.name);

  for (const s of servers) {
    const card = document.createElement("div");
    card.className = "card chartcard";
    card.id = "chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_");
    card.innerHTML =
      `<div class="charthead"><h2>${esc(s.name)}</h2>` +
      `<span class="badge" id="badge-${esc(s.name)}"></span></div>` +
      `<div class="chartwrap"><canvas></canvas></div>` +
      `<div class="chartlegend">` +
      `<span class="lg rx">▼ in ${fmt(0)}</span>` +
      `<span class="lg tx">▲ out ${fmt(0)}</span>` +
      `<span class="lg lat-title">⏱️ Latency</span></div>` +
      `<div class="chartwrap latwrap"><canvas id="lat-${esc(s.name).replace(/[^a-zA-Z0-9]/g, "_")}"></canvas></div>`;
    grid.appendChild(card);
  }

  for (const s of servers) {
    const canvas = grid.querySelector("#chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_") + " canvas");
    const ch = new Chart(canvas, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { label: "in", data: [], borderColor: COLORS.rx, backgroundColor: "rgba(34,211,238,.12)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
          { label: "out", data: [], borderColor: COLORS.tx, backgroundColor: "rgba(245,158,11,.10)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },   // eigener HTML-Tooltip (eingefroren, Maus-verankert)
        },
        scales: {
          x: { ticks: { color: "#64748b", maxTicksLimit: 6, maxRotation: 0 }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { ticks: { color: "#64748b", callback: v => fmt(v) }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
        interaction: { intersect: false, mode: "index" },
      },
    });
    state.charts[s.name] = ch;
    /* Latenz-Chart (Poll-Antwortzeit, 5 min Ring-Buffer) */
    const lc = grid.querySelector("#chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_") + " .latwrap canvas");
    const lch = new Chart(lc, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          label: "ms", data: [], borderColor: "#a78bfa",
          backgroundColor: "rgba(167,139,250,.12)", fill: true,
          tension: .3, pointRadius: 0, borderWidth: 1.5,
          spanGaps: true,
        }],
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: true } },
        scales: {
          x: { ticks: { color: "#64748b", maxTicksLimit: 4, maxRotation: 0 }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { ticks: { color: "#64748b", callback: v => v + " ms" }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
        interaction: { intersect: false, mode: "index" },
      },
    });
    state.latencyCharts[s.name] = lch;
    /* Eigener Tooltip: friert bei Hover die Daten ein, verankert an der Maus */
    canvas.addEventListener("mousemove", e => {
      const rect = canvas.getBoundingClientRect();
      const x = e.offsetX !== undefined ? e.offsetX : e.clientX - rect.left;
      const idx = chartIndexAt(ch, x);
      if (idx == null) { hideTip(); return; }
      const ser = state.lastSeries && state.lastSeries[s.name];
      let ts = null, snap = null;
      if (ser && ser.ts && ser.ts.length) {
        const cnt = Math.min(300, ser.ts.length);
        const seriesIdx = ser.ts.length - cnt + Math.min(idx, cnt - 1);
        ts = ser.ts[seriesIdx];
        snap = findSnap(s.name, ts);
      }
      frozenTip = { server: s.name };
      showTip(e.clientX, e.clientY, ts != null ? fmtTs(ts) : "",
        ch.data.datasets[0].data[idx] || 0,
        ch.data.datasets[1].data[idx] || 0, snap);
    });
    canvas.addEventListener("mouseleave", hideTip);
  }
}

function updateCharts(series) {
  const n = 300;
  let ymax = 0;
  if (state.equalScale) {
    // Globales Maximum ueber ALLE Server -> gleiche Y-Skala fuer Vergleich
    for (const name of state.servers) {
      const s = series[name];
      if (!s) continue;
      const cnt = Math.min(n, s.rx.length);
      for (let i = 0; i < cnt; i++) {
        const v = Math.max(s.rx[i] || 0, s.tx[i] || 0);
        if (v > ymax) ymax = v;
      }
    }
  }
  for (const name of state.servers) {
    const ch = state.charts[name];
    if (!ch) continue;
    const s = series[name] || { ts: [], rx: [], tx: [] };
    ch.data.labels = s.ts.slice(-n).map(fmtTs);
    ch.data.datasets[0].data = s.rx.slice(-n);
    ch.data.datasets[1].data = s.tx.slice(-n);
    if (state.equalScale) {
      ch.options.scales.y.min = 0;
      ch.options.scales.y.max = ymax > 0 ? ymax * 1.15 : undefined;
    } else {
      delete ch.options.scales.y.min;
      delete ch.options.scales.y.max;
    }
    ch.update("none");
  }
}

document.getElementById("scalebtn").addEventListener("click", () => {
  state.equalScale = !state.equalScale;
  document.getElementById("scalebtn").classList.toggle("active", state.equalScale);
  if (state.lastSeries) updateCharts(state.lastSeries);
  applyDetailScale();
});

/* ---------- Prozess-Detail-Grafik (Klick auf Tabellen-Zeile) ---------- */
function renderDetailCharts() {
  const grid = document.getElementById("procdetail");
  // Nicht mehr ausgewaehlte Server: Chart zerstoeren + DOM-Karte entfernen
  for (const s of Object.keys(state.detailCharts)) {
    if (!(s in state.detailProcs)) {
      if (state.detailCharts[s]) state.detailCharts[s].destroy();
      delete state.detailCharts[s];
    }
  }
  for (const card of [...grid.querySelectorAll(".chartcard")]) {
    if (!(card.dataset.detail in state.detailProcs)) card.remove();
  }
  const servers = state.servers.filter(s => s in state.detailProcs);
  if (servers.length === 0) {
    grid.style.display = "none";
    grid.innerHTML = "";
    return;
  }
  grid.style.display = "";
  for (const s of servers) {
    const proc = state.detailProcs[s];
    let card = grid.querySelector(`[data-detail="${esc(s)}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "card chartcard";
      card.dataset.detail = s;
      card.innerHTML = `<div class="charthead"><h2 class="dtitle"></h2><button class="dclose" title="Close">✕</button></div><div class="chartwrap"><canvas></canvas></div>`;
      card.querySelector(".dclose").addEventListener("click", () => {
        delete state.detailProcs[s];
        renderDetailCharts();
      });
      grid.appendChild(card);
    }
    card.querySelector(".dtitle").textContent = proc + " · " + s;
    card.style.display = state.visible[s] === false ? "none" : "";
    let ch = state.detailCharts[s];
    if (!ch) {
      const canvas = card.querySelector("canvas");
      const old = Chart.getChart(canvas);
      if (old) old.destroy();
      ch = new Chart(canvas, {
        type: "line",
        data: { labels: [], datasets: [
          { label: "in", data: [], borderColor: COLORS.rx, backgroundColor: "rgba(34,211,238,.12)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
          { label: "out", data: [], borderColor: COLORS.tx, backgroundColor: "rgba(245,158,11,.12)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }
        ]},
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: { enabled: false } },
          scales: {
            x: { ticks: { color: "#64748b", maxTicksLimit: 6, maxRotation: 0 }, grid: { color: "rgba(255,255,255,.05)" } },
            y: { min: 0, ticks: { color: "#64748b", callback: v => fmt(v) }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
          },
          interaction: { intersect: false, mode: "index" },
        }
      });
      state.detailCharts[s] = ch;
      /* Eigener Tooltip (wie Haupt-Charts): friert ein, Maus-verankert, nur in/out */
      canvas.addEventListener("mousemove", e => {
        const rect = canvas.getBoundingClientRect();
        const x = e.offsetX !== undefined ? e.offsetX : e.clientX - rect.left;
        const idx = chartIndexAt(ch, x);
        if (idx == null) { hideTip(); return; }
        showTip(e.clientX, e.clientY, ch.data.labels[idx] || "",
          ch.data.datasets[0].data[idx] || 0,
          ch.data.datasets[1].data[idx] || 0, null);
      });
      canvas.addEventListener("mouseleave", hideTip);
    }
    // History laden, wenn die Auswahl gewechselt hat
    if (ch._loaded !== proc) fetchDetailHistory(s, proc);
  }
}

function fetchDetailHistory(server, proc) {
  const ch = state.detailCharts[server];
  if (!ch) return;
  ch._loaded = proc;
  fetch(`/api/process_history?server=${encodeURIComponent(server)}&proc=${encodeURIComponent(proc)}`)
    .then(r => r.json())
    .then(d => {
      ch.data.labels = (d.ts || []).map(fmtTs);
      ch.data.datasets[0].data = d.rx || [];
      ch.data.datasets[1].data = d.tx || [];
      ch.update("none");
    })
    .catch(() => {});
}

document.getElementById("proctbody").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-proc]");
  if (!tr) return;
  const server = tr.dataset.server, proc = tr.dataset.proc;
  if (state.detailProcs[server] === proc) delete state.detailProcs[server];
  else state.detailProcs[server] = proc;
  renderDetailCharts();
});

/* ---------- Statusleiste + Karten ---------- */
function renderStatusbar(servers) {
  const ver = document.getElementById("ver");
  if (ver && state.version) ver.textContent = "v" + state.version;
  const bar = document.getElementById("statusbar");
  bar.innerHTML = servers.map(s => {
    const on = s.online;
    const t = s.totals || { rx: 0, tx: 0 };
    return `<div class="stat ${on ? "online" : "offline"}">` +
      `<div class="dot"></div>` +
      `<div class="statname">${esc(s.name)} <small>${esc(s.hostname || "")} · v${esc(s.version || "?")}</small></div>` +
      `<div class="statnums"><span class="rx">▼ ${fmt(t.rx)}</span>` +
      `<span class="tx">▲ ${fmt(t.tx)}</span></div>` +
      (s.error ? `<div class="err" title="${esc(s.error)}">offline</div>` : "") +
      `</div>`;
  }).join("");

  for (const s of servers) {
    const badge = document.getElementById("badge-" + esc(s.name));
    if (!badge) continue;
    const t = s.totals || { rx: 0, tx: 0 };
    badge.className = "badge " + (s.online ? "ok" : "bad");
    // Verbindungen dieses Servers aus der aktuellen Tabelle summieren
    let conns = 0;
    for (const r of (state.lastTable || [])) {
      const h = r.hosts && r.hosts[s.name];
      if (h && h.conns) conns += h.conns;
    }
    badge.innerHTML = s.online
      ? `<span class="bstat">online</span> <span class="bn">▼ ${fmt(t.rx)}</span> <span class="bn">▲ ${fmt(t.tx)}</span>` +
        (conns ? `<span class="bn conns">🔗 ${conns}</span>` : "") +
        `<span class="lat"></span>`
      : "offline";
    const lg = document.querySelectorAll("#chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_") + " .chartlegend .lg");
    if (lg.length === 2) { lg[0].textContent = "▼ in " + fmt(t.rx); lg[1].textContent = "▲ out " + fmt(t.tx); }
  }
}

/* ---------- Server-Filter (Checkbox-Chips: Tabelle + Charts) ---------- */
function syncAllChip(bar, servers) {
  const allCb = bar.querySelector("input[data-all]");
  if (!allCb) return;
  const n = servers.filter(s => state.visible[s.name]).length;
  allCb.checked = n === servers.length;
  allCb.indeterminate = n > 0 && n < servers.length;
}

function buildServerFilter(servers) {
  const bar = document.getElementById("serverfilter");
  bar.innerHTML = "";
  servers.forEach(s => { if (!(s.name in state.visible)) state.visible[s.name] = true; });

  const all = document.createElement("label");
  all.className = "chip";
  all.innerHTML = `<input type="checkbox" data-all checked><span>All</span>`;
  all.querySelector("input").addEventListener("change", e => {
    const v = e.target.checked;
    servers.forEach(s => state.visible[s.name] = v);
    bar.querySelectorAll("input[data-srv]").forEach(i => i.checked = v);
    syncAllChip(bar, servers);
    applyVisibility();
  });
  bar.appendChild(all);

  for (const s of servers) {
    const lab = document.createElement("label");
    lab.className = "chip";
    lab.innerHTML = `<input type="checkbox" data-srv="${esc(s.name)}" checked><span>${esc(s.name)}</span>`;
    lab.querySelector("input").addEventListener("change", e => {
      state.visible[s.name] = e.target.checked;
      syncAllChip(bar, servers);
      applyVisibility();
    });
    bar.appendChild(lab);
  }
}

function applyVisibility() {
  for (const name of state.servers) {
    const card = document.getElementById("chart-" + name.replace(/[^a-zA-Z0-9]/g, "_"));
    if (card) card.style.display = state.visible[name] ? "" : "none";
  }
  renderTable(state.lastTable || [], state.servers.map(n => ({ name: n })));
  renderDetailCharts();
}

/* Live-Werte an die Detail-Grafiken haengen (aus dem aktuellen Dashboard-Poll) */
function updateDetailCharts(d) {
  for (const s of Object.keys(state.detailProcs)) {
    const ch = state.detailCharts[s];
    if (!ch) continue;
    const proc = state.detailProcs[s];
    let rx = 0, tx = 0;
    for (const row of d.table || []) {
      const h = row.hosts && row.hosts[s];
      if (row.name === proc && h) { rx = h.rx ?? 0; tx = h.tx ?? 0; break; }
    }
    const max = 300;
    ch.data.labels.push(fmtTs(d.ts || (Date.now() / 1000)));
    ch.data.datasets[0].data.push(rx);
    ch.data.datasets[1].data.push(tx);
    if (ch.data.labels.length > max) {
      ch.data.labels.shift();
      ch.data.datasets[0].data.shift();
      ch.data.datasets[1].data.shift();
    }
  }
  applyDetailScale();
}

/* Gleiche Y-Skala fuer ALLE Detail-Grafiken (wenn Equal scale aktiv) */
function applyDetailScale() {
  let ymax = 0;
  for (const s of Object.keys(state.detailCharts)) {
    const ch = state.detailCharts[s];
    if (!ch) continue;
    for (const ds of ch.data.datasets) {
      for (const v of ds.data) if (v > ymax) ymax = v;
    }
  }
  for (const s of Object.keys(state.detailCharts)) {
    const ch = state.detailCharts[s];
    if (!ch) continue;
    if (state.equalScale) {
      ch.options.scales.y.min = 0;
      ch.options.scales.y.max = ymax > 0 ? ymax * 1.15 : undefined;
    } else {
      delete ch.options.scales.y.min;
      delete ch.options.scales.y.max;
    }
    ch.update("none");
  }
}

/* ---------- Process table: one row per (process x server) ---------- */
function buildTableHeader(servers) {
  const thead = document.getElementById("procthead");
  const cls = k => "sortable" +
    (state.sortKey === k ? " active" + (state.sortDir < 0 ? " sort-desc" : "") : "");
  thead.innerHTML = `<tr>` +
    `<th data-key="name" class="${cls("name")}">Process</th>` +
    `<th data-key="server" class="${cls("server")}">Server</th>` +
    `<th data-key="rx" class="sortable num ${state.sortKey === "rx" ? "active" + (state.sortDir < 0 ? " sort-desc" : "") : ""}">in</th>` +
    `<th data-key="tx" class="sortable num ${state.sortKey === "tx" ? "active" + (state.sortDir < 0 ? " sort-desc" : "") : ""}">out</th>` +
    `<th data-key="conn" class="sortable num ${state.sortKey === "conn" ? "active" + (state.sortDir < 0 ? " sort-desc" : "") : ""}" title="active TCP connections">conn</th>` +
    `</tr>`;
}

function renderTable(table, servers) {
  const tbody = document.getElementById("proctbody");
  const rows = [];
  for (const r of table) {
    for (const sname of Object.keys(r.hosts || {})) {
      if (!state.visible[sname]) continue;
      rows.push({ r, sname });
    }
  }
  rows.sort((a, b) => {
    let av, bv;
    const ha = a.r.hosts[a.sname] || {}, hb = b.r.hosts[b.sname] || {};
    if (state.sortKey === "name") { av = a.r.name.toLowerCase(); bv = b.r.name.toLowerCase(); }
    else if (state.sortKey === "server") { av = a.sname.toLowerCase(); bv = b.sname.toLowerCase(); }
    else if (state.sortKey === "rx") { av = ha.rx || 0; bv = hb.rx || 0; }
    else if (state.sortKey === "tx") { av = ha.tx || 0; bv = hb.tx || 0; }
    else if (state.sortKey === "conn") { av = ha.conns || 0; bv = hb.conns || 0; }
    else { av = (ha.rx || 0) + (ha.tx || 0); bv = (hb.rx || 0) + (hb.tx || 0); }
    if (av < bv) return -state.sortDir;
    if (av > bv) return state.sortDir;
    return 0;
  });

  tbody.innerHTML = rows.map(({ r, sname }) => {
    const isRest = r.kind === "rest";
    let badge = "";
    if (r.kind === "container") badge = `<span class="cont">Container</span>`;
    else if (r.container) badge = `<span class="cont">${esc(r.container)}</span>`;
    const h = r.hosts[sname] || {};
    return `<tr${isRest ? ' class="restrow"' : ""} data-server="${esc(sname)}" data-proc="${esc(r.name)}">` +
      `<td class="pname${isRest ? " rest" : ""}">${esc(r.name)}${badge}</td>` +
      `<td class="srv">${esc(sname)}</td>` +
      `<td class="num rx">${h.rx == null ? "–" : fmt(h.rx)}</td>` +
      `<td class="num tx">${h.tx == null ? "–" : fmt(h.tx)}</td>` +
      `<td class="num conn">${h.conns == null ? "–" : h.conns}</td></tr>`;
  }).join("");
}

/* ---------- Disk-I/O Tabelle: one row per (process x server) ---------- */
function buildDiskHeader(servers) {
  const thead = document.getElementById("diskthead");
  const cls = k => "sortable" +
    (state.diskSortKey === k ? " active" + (state.diskSortDir < 0 ? " sort-desc" : "") : "");
  thead.innerHTML = `<tr>` +
    `<th data-key="name" class="${cls("name")}">Process</th>` +
    `<th data-key="server" class="${cls("server")}">Server</th>` +
    `<th data-key="read" class="sortable num ${state.diskSortKey === "read" ? "active" + (state.diskSortDir < 0 ? " sort-desc" : "") : ""}">read</th>` +
    `<th data-key="write" class="sortable num ${state.diskSortKey === "write" ? "active" + (state.diskSortDir < 0 ? " sort-desc" : "") : ""}">write</th>` +
    `</tr>`;
}

function renderDiskTable(table, servers) {
  const tbody = document.getElementById("disktbody");
  const rows = [];
  for (const r of table) {
    for (const sname of Object.keys(r.hosts || {})) {
      if (!state.visible[sname]) continue;
      rows.push({ r, sname });
    }
  }
  rows.sort((a, b) => {
    let av, bv;
    const ha = a.r.hosts[a.sname] || {}, hb = b.r.hosts[b.sname] || {};
    const rd = h => (state.diskMode === "avg10" ? (h.read10 ?? h.read) : h.read) || 0;
    const wr = h => (state.diskMode === "avg10" ? (h.write10 ?? h.write) : h.write) || 0;
    if (state.diskSortKey === "name") { av = a.r.name.toLowerCase(); bv = b.r.name.toLowerCase(); }
    else if (state.diskSortKey === "server") { av = a.sname.toLowerCase(); bv = b.sname.toLowerCase(); }
    else if (state.diskSortKey === "read") { av = rd(ha); bv = rd(hb); }
    else if (state.diskSortKey === "write") { av = wr(ha); bv = wr(hb); }
    else { av = rd(ha) + wr(ha); bv = rd(hb) + wr(hb); }
    if (av < bv) return -state.diskSortDir;
    if (av > bv) return state.diskSortDir;
    return 0;
  });

  tbody.innerHTML = rows.map(({ r, sname }) => {
    let badge = "";
    if (r.container) badge = `<span class="cont">${esc(r.container)}</span>`;
    const h = r.hosts[sname] || {};
    const rd = state.diskMode === "avg10" ? (h.read10 ?? h.read) : h.read;
    const wr = state.diskMode === "avg10" ? (h.write10 ?? h.write) : h.write;
    return `<tr data-server="${esc(sname)}" data-proc="${esc(r.name)}">` +
      `<td class="pname">${esc(r.name)}${badge}</td>` +
      `<td class="srv">${esc(sname)}</td>` +
      `<td class="num rx">${rd == null ? "–" : fmt(rd)}</td>` +
      `<td class="num tx">${wr == null ? "–" : fmt(wr)}</td></tr>`;
  }).join("");
}

/* ---------- Storage (Füllstände Pools/Filesysteme) ---------- */
let storageData = null;
let lastStorageLoad = 0;
let storageCharts = {};  // key:serie -> Chart (für sauberes destroy beim Re-Render)
let storageMode = {};    // key -> "h24" | "d7" | "m" (gewählter Zeitbereich je Karte)
let storageScale = "full";  // "full" (0-100%) | "zoom" (Messbereich)
let storageOrder = [];   // Karten-Reihenfolge (Drag & Drop, localStorage)
try { storageOrder = JSON.parse(localStorage.getItem("netspy.storageOrder") || "[]"); } catch (e) { storageOrder = []; }

function stPct(size, used) { return size > 0 ? (used / size) * 100 : 0; }

function fmtBytes(b) {
  if (b == null || b < 0) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : 1) + " " + u[i];
}

async function loadStorage() {
  try {
    const r = await fetch("/api/storage");
    if (!r.ok) return;
    storageData = await r.json();
    renderStorage();
  } catch (e) { /* Offline/Start -> still */ }
}

async function storagePost(act, key) {
  try {
    const r = await fetch("/api/storage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: act, key })
    });
    if (r.ok) loadStorage();
  } catch (e) { /* still */ }
}

function renderStorage() {
  const grid = document.getElementById("storagegrid");
  const tbody = document.getElementById("storagetbody");
  if (!grid || !storageData) return;
  const { enabled, recorded, available, host_access } = storageData;
  const allKeys = [...new Set([...Object.keys(recorded || {}), ...Object.keys(available || {})])].sort();
  const enabledKeys = allKeys.filter(k => (enabled || []).includes(k));
  // Gespeicherte Drag&Drop-Reihenfolge anwenden (unbekannte Keys ans Ende)
  const orderedKeys = [...enabledKeys].sort((a, b) => {
    const ia = storageOrder.indexOf(a), ib = storageOrder.indexOf(b);
    return (ia === -1 ? 9999 : ia) - (ib === -1 ? 9999 : ib);
  });
  const noHost = !allKeys.length && host_access && Object.values(host_access).some(v => v === false);
  // --- Karten oben: NUR aktivierte (recording) Laufwerke ---
  Object.values(storageCharts).forEach(ch => { try { ch.destroy(); } catch (e) { /* still */ } });
  storageCharts = {};
  if (!enabledKeys.length) {
    grid.innerHTML = noHost
      ? `<p class="hint" style="color:#fbbf24">⚠️ <b>No host access</b> — the container cannot read the host mounts. It must run with <code>--pid=host</code> (host PID namespace): in Unraid go to <b>Docker → NetSpy → Edit → Apply</b> (or <b>Reinstall</b>) so the change takes effect.</p>`
      : `<p class="hint">No drives recording — activate one in the list below.</p>`;
  } else {
    grid.innerHTML = orderedKeys.map(key => {
      const rec = (recorded || {})[key] || {};
      const av = (available || {})[key] || {};
      const name = rec.name || av.name || key;
      const server = rec.server || av.server || "";
      const size = rec.size || av.size || 0;
      const used = rec.used != null ? rec.used : (av.used || 0);
      const p = stPct(size, used);
      const gone = !av.name;
      const isRec = true;  // Karten zeigen nur enabled Laufwerke
      const hasData = (rec.h24 && rec.h24.length) || (rec.d7 && rec.d7.length);
      // Auffüll-Markierung: gestrichelte Linie wenn (noch) keine Realdaten
      const filled = !((rec.h24 && rec.h24.length) || (rec.d7 && rec.d7.length) || (rec.m && rec.m.length));
      const sname = esc(server), sn = esc(name), skey = esc(key);
      const mode = storageMode[key] || "h24";
      const modeBtn = m => `<button class="chip-btn ${mode === m ? "active" : ""}" data-mode="${m}" data-key="${skey}">${m === "h24" ? "24 h" : m === "d7" ? "7 d" : "12 m"}</button>`;
      return `<div class="stcard${gone ? " gone" : ""}" data-key="${skey}" draggable="true" title="drag to reorder">
        <div class="sthead">
          <span class="stname">${sn}</span>
          <span class="cont">${sname}</span>
          ${gone ? `<span class="stgone" title="no longer visible — data kept until you delete it">⚠️ missing</span>` : ""}
          ${filled ? `<span class="stgone" title="no history yet — dashed line is the current value projected, not real data">⏳ estimated</span>` : ""}
          <span class="stfill ${p > 90 ? "bad" : p > 75 ? "warn" : ""}">${p.toFixed(0)}%</span>
          <span class="sthint">${fmtBytes(used)} / ${fmtBytes(size)}</span>
        </div>
        <div class="stbar"><div class="stbar-fill" style="width:${Math.min(p, 100)}%"></div></div>
        <div class="stchartbig"><canvas data-k="${skey}" data-s="${mode}"></canvas></div>
        <div class="stmodes">${modeBtn("h24")}${modeBtn("d7")}${modeBtn("m")}</div>
        <div class="stactions">
          <button class="chip-btn ${isRec ? "active" : ""}" data-act="toggle" data-key="${skey}">${isRec ? "⏹ stop recording" : "⏺ record"}</button>
          ${hasData ? `<button class="chip-btn danger" data-act="delete" data-key="${skey}">🗑️ delete data</button>` : ""}
        </div>
      </div>`;
    }).join("");
    // Große Linien-Grafik pro Karte (wie die Netzwerk-Charts), Serie je Modus
    grid.querySelectorAll(".stchartbig canvas").forEach(c => {
      const key = c.dataset.k, serie = c.dataset.s;
      const rec = (recorded || {})[key] || {};
      const points = (serie === "h24" ? rec.h24 : serie === "d7" ? rec.d7 : rec.m) || [];
      const size = rec.size || ((available || {})[key] || {}).size || 0;
      const usedNow = (rec.used != null ? rec.used : ((available || {})[key] || {}).used) || 0;
      const old = Chart.getChart(c);
      if (old) old.destroy();
      if (!size) return;  // ohne Größe keine sinnvolle Chart
      const labels = points.map(pp => new Date(pp[0] * 1000)
        .toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }));
      const data = points.map(pp => size > 0 ? +((pp[1] / size) * 100).toFixed(1) : 0);
      // Fehlende Historie mit dem aktuellen Wert auffüllen (gestrichelt =
      // projiziert, KEINE Realdaten). Die Linie läuft wie bei den Netzwerk-
      // Graphen von links (Vergangenheit) bis rechts (jetzt): Lücken VOR den
      // ersten echten Daten UND NACH dem letzten echten Punkt bis jetzt.
      const nowMs = Date.now();
      const horizon = serie === "h24" ? 24 * 3600 : serie === "d7" ? 7 * 86400 : 360 * 86400;
      const step = serie === "h24" ? 3600 : serie === "d7" ? 86400 : 30 * 86400;
      const curVal = data.length ? data[data.length - 1] : (size > 0 ? +((usedNow / size) * 100).toFixed(1) : 0);
      const firstReal = points.length ? points[0][0] * 1000 : nowMs;
      const lastReal = points.length ? points[points.length - 1][0] * 1000 : nowMs;
      const fmtL = t => new Date(t).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      const fillVorL = [], fillVorD = [], fillNachL = [], fillNachD = [];
      if (points.length < (horizon / step)) {
        for (let t = nowMs - horizon * 1000; t < firstReal; t += step * 1000) {
          fillVorL.push(fmtL(t)); fillVorD.push(curVal);
        }
        for (let t = lastReal + step * 1000; t < nowMs; t += step * 1000) {
          fillNachL.push(fmtL(t)); fillNachD.push(curVal);
        }
      }
      const nv = fillVorL.length, nr = labels.length, nn = fillNachL.length;
      const datasets = [];
      if (nv) {
        datasets.push({ data: [...fillVorD, ...Array(nr + nn).fill(null)],
          borderColor: "rgba(245,158,11,.35)", backgroundColor: "rgba(245,158,11,.04)",
          fill: true, pointRadius: 0, tension: .25, borderWidth: 1.5, borderDash: [4, 4] });
      }
      datasets.push({ data: [...Array(nv).fill(null), ...data, ...Array(nn).fill(null)],
        borderColor: "#f59e0b", backgroundColor: "rgba(245,158,11,.12)",
        fill: true, pointRadius: 0, tension: .25, borderWidth: 2 });
      if (nn) {
        datasets.push({ data: [...Array(nv + nr).fill(null), ...fillNachD],
          borderColor: "rgba(245,158,11,.35)", backgroundColor: "rgba(245,158,11,.04)",
          fill: true, pointRadius: 0, tension: .25, borderWidth: 1.5, borderDash: [4, 4] });
      }
      // Y-Achse: full = 0-100%; zoom = Messbereich + Puffer (kleine Änderungen sichtbar)
      const allVals = [...fillVorD, ...data, ...fillNachD].filter(v => v != null && isFinite(v));
      let yMin = 0, yMax = 100;
      if (storageScale === "zoom" && allVals.length) {
        const mn = Math.min(...allVals), mx = Math.max(...allVals);
        const pad = Math.max((mx - mn) * 0.2, 1);
        yMin = Math.max(0, Math.floor(mn - pad));
        yMax = Math.ceil(mx + pad);
        if (yMax - yMin < 2) yMax = yMin + 2;
      }
      storageCharts[key + ":" + serie] = new Chart(c, {
        type: "line",
        data: { labels: [...fillVorL, ...labels, ...fillNachL], datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false },
            tooltip: { callbacks: { label: ctx => ctx.parsed.y + " %" } } },
          scales: {
            x: { ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 6, font: { size: 10 } } },
            y: { min: yMin, max: yMax, ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 5, font: { size: 10 }, callback: v => v + "%" } }
          }
        }
      });
    });
    // Modus-Umschalter (24 h / 7 d / months)
    grid.querySelectorAll(".stmodes [data-mode]").forEach(b => b.addEventListener("click", () => {
      storageMode[b.dataset.key] = b.dataset.mode;
      renderStorage();
    }));
    // Drag & Drop: Karten umsortieren (Reihenfolge in localStorage)
    let dragKey = null;
    grid.querySelectorAll(".stcard").forEach(card => {
      card.addEventListener("dragstart", e => {
        dragKey = card.dataset.key;
        e.dataTransfer.effectAllowed = "move";
        card.style.opacity = ".5";
      });
      card.addEventListener("dragend", () => { card.style.opacity = ""; });
      card.addEventListener("dragover", e => e.preventDefault());
      card.addEventListener("drop", e => {
        e.preventDefault();
        const targetKey = card.dataset.key;
        if (!dragKey || dragKey === targetKey) return;
        // Aktuelle Anzeige-Reihenfolge als Basis (nicht nur gespeicherte)
        const current = [...grid.querySelectorAll(".stcard")].map(c => c.dataset.key);
        const list = current.filter(k => k !== dragKey);
        const to = Math.max(0, list.indexOf(targetKey));
        list.splice(to, 0, dragKey);
        storageOrder = list;
        try { localStorage.setItem("netspy.storageOrder", JSON.stringify(storageOrder)); } catch (err) { /* still */ }
        renderStorage();
      });
    });
    // Karten-Buttons: record / delete
    grid.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => {
      storagePost(b.dataset.act, b.dataset.key);
    }));
  }
  // --- Dateibrowser: eigener Baum pro Server, alles anfangs eingeklappt ---
  const treeEl = document.getElementById("storagetree");
  if (!treeEl) return;
  if (!allKeys.length) {
    treeEl.innerHTML = `<p class="hint">No drives detected yet — agents report storage every 60 s.</p>`;
    return;
  }
  let stExpanded = [];
  try { stExpanded = JSON.parse(localStorage.getItem("netspy.storageExpanded") || "[]"); } catch (e) { /* still */ }
  const expSet = new Set(stExpanded);   // Startzustand: alles zugeklappt
  // Server ermitteln (aus available + recorded), stabil alphabetisch sortiert
  const srvNames = [...new Set(allKeys.map(k => k.includes(":") ? k.split(":")[0] : "(unknown)"))].sort();
  // Kinder eines Knotens NUR im eigenen Server-Baum (parent = Pfad des Knotens)
  const stKidsOf = (server, path) => allKeys
    .filter(k => k.startsWith(server + ":") && (((available || {})[k] || {}).parent || null) === (path || null))
    .sort((a, b) => (((available || {})[a] || {}).name || a).localeCompare(((available || {})[b] || {}).name || b));
  // Label: letzter Pfadteil (Ordner-Name); bei "/" der Device-Name (z. B. boot-pool)
  const stLabel = k => {
    const av = (available || {})[k] || {};
    const p = av.path || k;
    if (p === "/") return av.name || k;
    return p.split("/").filter(Boolean).pop() || p;
  };
  const stRows = (server, path, depth) => {
    const kids = stKidsOf(server, path);
    if (!kids.length) return "";
    let html = "";
    for (const k of kids) {
      const rec = (recorded || {})[k] || {};
      const av = (available || {})[k] || {};
      const name = rec.name || av.name || k;
      const size = rec.size || av.size || 0;
      const used = rec.used != null ? rec.used : (av.used || 0);
      const p = stPct(size, used);
      const gone = !av.name;
      const isRec = (enabled || []).includes(k);
      const hasData = (rec.h24 && rec.h24.length) || (rec.d7 && rec.d7.length);
      const hasKids = stKidsOf(server, av.path || k).length > 0;
      const open = expSet.has(k);
      const pctStyle = p > 90 ? "color:#f87171" : p > 75 ? "color:#fbbf24" : "";
      const tw = hasKids
        ? `<span class="sttw" data-tw="${esc(k)}" title="expand">${open ? "▾" : "▸"}</span>`
        : `<span class="sttw"></span>`;
      html += `<tr class="${gone ? "restrow" : ""}" data-key="${esc(k)}">
        <td class="pname" style="padding-left:${depth * 18}px">
          ${tw}${esc(stLabel(k))}
          ${av.type === "zfs" && (av.path || k) !== "/" && name !== stLabel(k) ? ` <span class="stsub" title="${esc(name)}">${esc(name.split("/").slice(0, -1).join("/"))}</span>` : ""}
          ${gone ? ` <span class="stgone" title="no longer visible — data kept until you delete it">⚠️</span>` : ""}
        </td>
        <td class="num" style="${pctStyle}">${p.toFixed(0)}%</td>
        <td class="num">${fmtBytes(used)} / ${fmtBytes(size)}</td>
        <td class="num" style="white-space:nowrap">
          <button class="chip-btn ${isRec ? "active" : ""}" data-act="toggle" data-key="${esc(k)}">${isRec ? "⏹ stop" : "⏺ record"}</button>
          ${hasData ? `<button class="chip-btn danger" data-act="delete" data-key="${esc(k)}" title="delete data">🗑️</button>` : ""}
        </td>
      </tr>`;
      if (open) html += stRows(server, av.path || k, depth + 1);
    }
    return html;
  };
  treeEl.innerHTML = srvNames.map(srv => {
    const noHost = (host_access || {})[srv] === false;
    const cnt = allKeys.filter(k => k.startsWith(srv + ":")).length;
    const body = stRows(srv, null, 0) || `<tr><td colspan="4" class="hint">No drives found.</td></tr>`;
    return `<details class="stsec" open>
      <summary>🖥️ <b>${esc(srv)}</b> <span class="hint">(${cnt} entr${cnt === 1 ? "y" : "ies"}${noHost ? " · ⚠️ no host access" : ""})</span></summary>
      ${noHost ? `<p class="hint" style="color:#fbbf24">⚠️ <b>No host access</b> — the container cannot read this server's mounts. It must run with <code>--pid=host</code> (host PID namespace).</p>` : ""}
      <div class="tablewrap">
        <table class="sttable">
          <thead><tr><th>Name</th><th class="num">Fill</th><th class="num">Used / Size</th><th style="width:180px"></th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </details>`;
  }).join("");
  // Ausklappen / Einklappen (Server-übergreifend, pro Key)
  treeEl.querySelectorAll("[data-tw]").forEach(sp => sp.addEventListener("click", () => {
    const k = sp.dataset.tw;
    if (expSet.has(k)) expSet.delete(k); else expSet.add(k);
    try { localStorage.setItem("netspy.storageExpanded", JSON.stringify([...expSet])); } catch (e) { /* still */ }
    renderStorage();
  }));
  // Buttons: record / delete (auch auf tiefen Ebenen)
  treeEl.querySelectorAll("[data-act]").forEach(b => b.addEventListener("click", () => {
    storagePost(b.dataset.act, b.dataset.key);
  }));
}

/* ---------- CPU/RAM Tabelle: one row per (process x server) ---------- */
function buildSysHeader(servers) {
  const thead = document.getElementById("systhead");
  const cls = k => "sortable" +
    (state.sysSortKey === k ? " active" + (state.sysSortDir < 0 ? " sort-desc" : "") : "");
  thead.innerHTML = `<tr>` +
    `<th data-key="name" class="${cls("name")}">Process</th>` +
    `<th data-key="server" class="${cls("server")}">Server</th>` +
    `<th data-key="cpu" class="sortable num ${state.sysSortKey === "cpu" ? "active" + (state.sysSortDir < 0 ? " sort-desc" : "") : ""}">CPU%</th>` +
    `<th data-key="mem" class="sortable num ${state.sysSortKey === "mem" ? "active" + (state.sysSortDir < 0 ? " sort-desc" : "") : ""}">RAM</th>` +
    `</tr>`;
}

function renderSysHosts(host_sys, servers) {
  const el = document.getElementById("syshosts");
  if (!el) return;
  el.innerHTML = servers.map(s => {
    const h = (host_sys && host_sys[s.name]) || {};
    const memPct = h.mem_total > 0 ? Math.round(((h.mem_used || 0) / h.mem_total) * 100) : 0;
    const cpuV = state.cpuMode === "avg10" ? h.cpu10 : h.cpu;
    return `<span class="syschip" title="Host total">${esc(s.name)}: ` +
      `<b style="color:#22d3ee">CPU ${cpuV == null ? "–" : cpuV + "%"}</b> · ` +
      `<b style="color:#a78bfa">RAM ${fmt(h.mem_used || 0)} / ${fmt(h.mem_total || 0)} (${memPct}%)</b></span>`;
  }).join(" ");
}

function renderSysTable(table, servers) {
  const tbody = document.getElementById("systbody");
  const rows = [];
  for (const r of table) {
    for (const sname of Object.keys(r.hosts || {})) {
      if (!state.visible[sname]) continue;
      rows.push({ r, sname });
    }
  }
  rows.sort((a, b) => {
    let av, bv;
    const ha = a.r.hosts[a.sname] || {}, hb = b.r.hosts[b.sname] || {};
    const cpuOf = h => (state.cpuMode === "avg10" ? (h.cpu10 ?? h.cpu) : h.cpu) || 0;
    if (state.sysSortKey === "name") { av = a.r.name.toLowerCase(); bv = b.r.name.toLowerCase(); }
    else if (state.sysSortKey === "server") { av = a.sname.toLowerCase(); bv = b.sname.toLowerCase(); }
    else if (state.sysSortKey === "mem") { av = ha.mem || 0; bv = hb.mem || 0; }
    else { av = cpuOf(ha); bv = cpuOf(hb); }
    if (av < bv) return -state.sysSortDir;
    if (av > bv) return state.sysSortDir;
    return 0;
  });

  tbody.innerHTML = rows.map(({ r, sname }) => {
    let badge = "";
    if (r.container) badge = `<span class="cont">${esc(r.container)}</span>`;
    const h = r.hosts[sname] || {};
    const cpuV = state.cpuMode === "avg10" ? (h.cpu10 ?? h.cpu) : h.cpu;
    return `<tr data-server="${esc(sname)}" data-proc="${esc(r.name)}">` +
      `<td class="pname">${esc(r.name)}${badge}</td>` +
      `<td class="srv">${esc(sname)}</td>` +
      `<td class="num cpu">${cpuV == null ? "–" : cpuV.toFixed(1) + " %"}</td>` +
      `<td class="num mem">${h.mem == null ? "–" : fmt(h.mem)}</td></tr>`;
  }).join("");
}

/* ---------- Latenz (Poll-Antwortzeit pro Server) ---------- */
function updateLatency(latency) {
  if (!latency) return;
  for (const sname of Object.keys(latency)) {
    const ch = state.latencyCharts && state.latencyCharts[sname];
    if (ch) {
      const L = latency[sname] || { ts: [], ms: [] };
      ch.data.labels = (L.ts || []).map(fmtTs);
      ch.data.datasets[0].data = (L.ms || []).map(v => (v < 0 ? null : v));
      ch.update("none");
    }
    const badge = document.getElementById("badge-" + esc(sname));
    if (!badge) continue;
    const arr = (latency[sname] && latency[sname].ms) || [];
    let last = null;
    for (let i = arr.length - 1; i >= 0; i--) { if (arr[i] >= 0) { last = arr[i]; break; } }
    if (last == null) continue;
    const cls = last < 100 ? "lat-ok" : (last < 500 ? "lat-warn" : "lat-bad");
    let b = badge.querySelector(".lat");
    if (!b) {
      b = document.createElement("span");
      b.className = "lat";
      badge.appendChild(b);
    }
    b.className = "lat " + cls;
    b.textContent = "⏱️ " + last.toFixed(0) + " ms";
  }
}

/* ---------- Interfaces (einklappbar, offen-Status + Sortierung bleiben erhalten) ---------- */
function renderIfaces(ifaces, servers) {
  const wrap = document.getElementById("ifaces");
  const wasOpen = {};
  wrap.querySelectorAll("details").forEach(d => { wasOpen[d.dataset.server] = d.open; });
  wrap.innerHTML = servers.map(s => {
    const st = state.ifaceSort[s.name] || { key: "name", dir: 1 };
    const list = [...(ifaces[s.name] || [])].sort((a, b) => {
      let av, bv;
      if (st.key === "name") {
        av = (a.container || a.name).toLowerCase();
        bv = (b.container || b.name).toLowerCase();
      }
      else { av = a[st.key] || 0; bv = b[st.key] || 0; }
      if (av < bv) return -st.dir;
      if (av > bv) return st.dir;
      return 0;
    });
    const cls = k => "sortable" + (k !== "name" ? " num" : "") +
      (st.key === k ? " active" + (st.dir < 0 ? " sort-desc" : "") : "");
    const rows = list.map(i => {
      // veth-Interfaces mit Container-Zuordnung zeigen den Docker-Namen
      const label = i.container ? esc(i.container) : esc(i.name);
      const sub = i.container ? ` <span class="vethsub">${esc(i.name)}</span>` : "";
      return `<tr><td class="pname"${i.container ? ` title="${esc(i.name)}"` : ""}>${label}${sub}` +
        (i.uplink ? ' <span class="uplink">UPLINK</span>' : "") +
        (i.container ? ` <span class="cont">Container</span>` : "") + `</td>` +
        `<td class="num rx">${fmt(i.rx)}</td><td class="num tx">${fmt(i.tx)}</td></tr>`;
    }).join("");
    return `<details class="card" data-server="${esc(s.name)}"${wasOpen[s.name] ? " open" : ""}>` +
      `<summary>Interfaces · ${esc(s.name)} (${list.length}) <span class="hint">— click header to sort</span></summary>` +
      `<table class="ifacetable"><thead><tr>` +
      `<th class="${cls("name")}" data-key="name">Interface</th>` +
      `<th class="${cls("rx")}" data-key="rx">in</th>` +
      `<th class="${cls("tx")}" data-key="tx">out</th>` +
      `</tr></thead><tbody>${rows}</tbody></table></details>`;
  }).join("");
}

document.getElementById("ifaces").addEventListener("click", e => {
  const th = e.target.closest("th[data-key]");
  const det = e.target.closest("details");
  if (!th || !det) return;
  const sname = det.dataset.server;
  const key = th.dataset.key;
  const st = state.ifaceSort[sname] || { key: "name", dir: 1 };
  if (st.key === key) st.dir *= -1;
  else { st.key = key; st.dir = key === "name" ? 1 : -1; }
  state.ifaceSort[sname] = st;
  if (state.lastIfaces) {
    renderIfaces(state.lastIfaces, state.servers.map(n => ({ name: n })));
  }
});

/* ---------- Hauptschleife ---------- */
async function refresh() {
  let d;
  try {
    d = await (await fetch("/api/dashboard")).json();
  } catch (e) {
    return;
  }
  if (!state.charts[state.servers[0]] || state.servers.length !== d.servers.length ||
      state.servers.some((n, i) => n !== d.servers[i].name)) {
    buildCharts(d.servers);
    buildTableHeader(d.servers);
    buildDiskHeader(d.servers);
    buildSysHeader(d.servers);
    buildServerFilter(d.servers);
  }
  state.version = d.version || state.version;
  state.lastIfaces = d.ifaces;
  state.lastTable = d.table;
  state.lastDisk = d.disk || [];
  state.lastSys = d.system || [];
  state.lastHostSys = d.host_sys || {};
  state.lastLatency = d.latency || {};
  state.lastServers = d.servers || [];
  state.lastSeries = d.series;
  /* Prozess-History pro Server (eingefrorene Hover-Werte, synchron zu den Chart-ts) */
  for (const srv of d.servers) {
    const ser = d.series && d.series[srv.name];
    const tsSnap = ser && ser.ts && ser.ts.length ? ser.ts[ser.ts.length - 1] : null;
    if (tsSnap == null) continue;
    const arr = state.procHistory[srv.name] || (state.procHistory[srv.name] = []);
    if (arr.length && tsSnap <= arr[arr.length - 1].ts) continue; // schon vorhanden
    const procs = (d.table || [])
      .filter(r => r.hosts && r.hosts[srv.name] &&
        ((r.hosts[srv.name].rx || 0) > 0 || (r.hosts[srv.name].tx || 0) > 0))
      .map(r => [r.name, r.container || null, r.hosts[srv.name].rx || 0, r.hosts[srv.name].tx || 0]);
    arr.push({ ts: tsSnap, procs });
    if (arr.length > 420) arr.splice(0, arr.length - 420); // ~7 min Puffer
  }
  renderStatusbar(d.servers);
  updateCharts(d.series);
  renderTable(d.table, d.servers);
  renderDiskTable(d.disk || [], d.servers);
  renderSysTable(d.system || [], d.servers);
  renderSysHosts(d.host_sys || {}, d.servers);
  updateLatency(d.latency);
  updateDetailCharts(d);
  renderIfaces(d.ifaces, d.servers);
}

document.getElementById("procthead").addEventListener("click", e => {
  const th = e.target.closest("th[data-key]");
  if (!th) return;
  const key = th.dataset.key;
  if (state.sortKey === key) state.sortDir *= -1;
  else { state.sortKey = key; state.sortDir = key === "name" || key === "server" ? 1 : -1; }
  buildTableHeader(state.servers.map(n => ({ name: n })));
  if (state.lastTable) renderTable(state.lastTable, state.servers.map(n => ({ name: n })));
});

document.getElementById("diskthead").addEventListener("click", e => {
  const th = e.target.closest("th[data-key]");
  if (!th) return;
  const key = th.dataset.key;
  if (state.diskSortKey === key) state.diskSortDir *= -1;
  else { state.diskSortKey = key; state.diskSortDir = key === "name" || key === "server" ? 1 : -1; }
  buildDiskHeader(state.servers.map(n => ({ name: n })));
  if (state.lastDisk) renderDiskTable(state.lastDisk, state.servers.map(n => ({ name: n })));
});

document.getElementById("systhead").addEventListener("click", e => {
  const th = e.target.closest("th[data-key]");
  if (!th) return;
  const key = th.dataset.key;
  if (state.sysSortKey === key) state.sysSortDir *= -1;
  else { state.sysSortKey = key; state.sysSortDir = key === "name" || key === "server" ? 1 : -1; }
  buildSysHeader(state.servers.map(n => ({ name: n })));
  if (state.lastSys) renderSysTable(state.lastSys, state.servers.map(n => ({ name: n })));
});

function setCpuMode(mode) {
  state.cpuMode = mode;
  const live = document.getElementById("cpumode-live");
  const avg = document.getElementById("cpumode-avg10");
  if (live) live.classList.toggle("active", mode === "live");
  if (avg) avg.classList.toggle("active", mode === "avg10");
  renderSysHosts(state.lastHostSys, state.lastServers);
  renderSysTable(state.lastSys, state.lastServers);
}
document.getElementById("cpumode-live").addEventListener("click", () => setCpuMode("live"));
document.getElementById("cpumode-avg10").addEventListener("click", () => setCpuMode("avg10"));

function setDiskMode(mode) {
  state.diskMode = mode;
  const live = document.getElementById("diskmode-live");
  const avg = document.getElementById("diskmode-avg10");
  if (live) live.classList.toggle("active", mode === "live");
  if (avg) avg.classList.toggle("active", mode === "avg10");
  renderDiskTable(state.lastDisk, state.lastServers);
}
document.getElementById("diskmode-live").addEventListener("click", () => setDiskMode("live"));
document.getElementById("diskmode-avg10").addEventListener("click", () => setDiskMode("avg10"));

function setStorageScale(mode) {
  storageScale = mode;
  const f = document.getElementById("stscale-full"), z = document.getElementById("stscale-zoom");
  if (f) f.classList.toggle("active", mode === "full");
  if (z) z.classList.toggle("active", mode === "zoom");
  renderStorage();
}
document.getElementById("stscale-full").addEventListener("click", () => setStorageScale("full"));
document.getElementById("stscale-zoom").addEventListener("click", () => setStorageScale("zoom"));

/* Tab-Umschaltung: Network / Disk / CPU-RAM / Storage / Settings */
document.getElementById("tabbtn-net").addEventListener("click", () => setTab("net"));
document.getElementById("tabbtn-disk").addEventListener("click", () => setTab("disk"));
document.getElementById("tabbtn-sys").addEventListener("click", () => setTab("sys"));
document.getElementById("tabbtn-storage").addEventListener("click", () => setTab("storage"));
document.getElementById("tabbtn-settings").addEventListener("click", () => setTab("settings"));

const TAB_IDS = ["net", "disk", "sys", "storage", "settings"];
function setTab(which) {
  try { localStorage.setItem("netspy.tab", which); } catch (e) { /* still */ }
  for (const t of TAB_IDS) {
    document.getElementById("panel-" + t).classList.toggle("hidden", t !== which);
    document.getElementById("tabbtn-" + t).classList.toggle("active", t === which);
    document.getElementById("tabbtn-" + t).setAttribute("aria-selected", t === which ? "true" : "false");
  }
  if (which === "settings") loadSettings();
  /* Chart-Groessen nach Layout-Wechsel neu berechnen */
  for (const s of Object.keys(state.charts)) {
    const ch = state.charts[s];
    if (ch) setTimeout(() => ch.resize(), 30);
  }
  for (const s of Object.keys(state.latencyCharts)) {
    const ch = state.latencyCharts[s];
    if (ch) setTimeout(() => ch.resize(), 30);
  }
}

/* Beim Laden den zuletzt aktiven Tab wiederherstellen (Reload bleibt im Tab) */
(function restoreTab() {
  try {
    const saved = localStorage.getItem("netspy.tab");
    if (saved && TAB_IDS.includes(saved)) setTab(saved);
  } catch (e) { /* still */ }
})();

/* ---------- Settings (Server-Verwaltung, servers.yaml mit Volume-Fallback) ---------- */
let settingsData = null;
let settingsStatus = "";
let settingsError = "";

async function loadSettings() {
  settingsError = "";
  try {
    const r = await fetch("/api/settings");
    settingsData = await r.json();
  } catch (e) {
    settingsData = null;
  }
  renderSettings();
}

function renderSettings() {
  const box = document.getElementById("settingsbox");
  if (!box) return;
  if (!settingsData) {
    box.innerHTML = `<p class="hint">Settings could not be loaded.</p>`;
    return;
  }
  const warn = settingsData.writable ? "" :
    `<div class="sett-warn">⚠️ <b>Config folder is not a mounted volume:</b> <code>${esc(settingsData.path)}</code><br>
      Saving there would not survive an update. Mount a volume — in the
      <code>docker-compose.yml</code> of the NetSpy web container (Unraid example,
      create the directory first):<br>
      <code>&nbsp;&nbsp;volumes:<br>&nbsp;&nbsp;&nbsp;&nbsp;- /mnt/user/appdata/netspy:/netspy</code><br>
      The <code>/netspy/config</code> subfolder (and later e.g. <code>/netspy/data</code>)
      is created automatically by the container.<br>
      For other systems e.g. <code>- /opt/netspy:/netspy</code>. Then recreate the
      container (<code>docker compose up -d</code> / redeploy the stack). The server list
      is stored as human-editable <code>servers.yaml</code>.<br>
      Without a volume the <code>SERVERS</code> environment variable stays active (fallback).</div>`;
  const okBanner = (settingsData.writable && settingsData.source === "env" && settingsData.has_template) ?
    `<div class="sett-ok">✅ <b>Volume detected</b> — template created: <code>${esc(settingsData.path)}</code><br>
      The file now lives on the host (visible proof). Servers still come from the
      <code>SERVERS</code> environment variable — add servers via the UI below
      or edit <code>servers.yaml</code> directly.</div>` : "";
  const errBanner = settingsError ? (
    `<div class="sett-warn"><b>❌ NOT SAVED</b><br>` +
    esc(settingsError).replace(/\n/g, "<br>") + `</div>`
  ) : "";
  const src = settingsData.source === "file"
    ? "servers.yaml (file)"
    : "SERVERS env var (fallback)";
  const srcBadge = settingsData.source === "file"
    ? `<span class="srcbadge src-file" title="Server list is read from the config file">📄 config file</span>`
    : `<span class="srcbadge src-env" title="Server list is read from the SERVERS environment variable">⚙️ env (SERVERS)</span>`;
  const rows = (settingsData.servers || []).map((s, i) =>
    `<div class="sett-row" data-origin="${s.origin || "config"}" data-original="${esc(s.name)}|${esc(s.url || "local")}">
       <span class="origin ${s.origin === "env" ? "o-env" : "o-config"}">${s.origin === "env" ? "env" : "config"}</span>
       <input class="sett-name" placeholder="Name (e.g. Unraid)" value="${esc(s.name)}">
       <input class="sett-url" placeholder="URL, 'local' or empty (=local)" value="${esc(s.url || "local")}">
       ${s.origin === "env" ? "" : `<button class="sett-del" title="Remove">✕</button>`}
     </div>`).join("");
  box.innerHTML =
    srcBadge +
    errBanner +
    okBanner +
    warn +
    `<p class="hint">Source: <b>${src}</b> · File: <code>${esc(settingsData.path)}</code></p>` +
    `<div id="settlist">${rows}</div>` +
    `<div class="sett-actions">
       <button id="sett-add">+ Add server</button>
       <button id="sett-save" class="primary">💾 Save</button>
       <span id="sett-status" class="hint">${esc(settingsStatus)}</span>
     </div>`;
  box.querySelector("#sett-add").addEventListener("click", () => {
    const list = document.getElementById("settlist");
    const div = document.createElement("div");
    div.className = "sett-row";
    div.dataset.origin = "config";
    div.innerHTML = `<span class="origin o-config">config</span>` +
      `<input class="sett-name" placeholder="Name (e.g. Unraid)">` +
      `<input class="sett-url" placeholder="URL, 'local' or empty">` +
      `<button class="sett-del" title="Remove">✕</button>`;
    div.querySelector(".sett-del").addEventListener("click", () => div.remove());
    list.appendChild(div);
  });
  box.querySelectorAll(".sett-del").forEach(b =>
    b.addEventListener("click", e => e.target.closest(".sett-row").remove()));
  // Env-Zeile wird zur config-Zeile, sobald der User sie aendert (bewusste Uebernahme)
  box.querySelectorAll(".sett-row[data-origin='env'] input").forEach(inp => {
    inp.addEventListener("input", () => {
      const row = inp.closest(".sett-row");
      if (row.dataset.origin !== "env") return;
      row.dataset.origin = "config";
      const lbl = row.querySelector(".origin");
      if (lbl) { lbl.className = "origin o-config"; lbl.textContent = "config"; }
      if (!row.querySelector(".sett-del")) {
        const del = document.createElement("button");
        del.className = "sett-del"; del.title = "Remove"; del.textContent = "✕";
        del.addEventListener("click", () => row.remove());
        row.appendChild(del);
      }
    });
  });
  box.querySelector("#sett-save").addEventListener("click", saveSettings);
}

async function saveSettings() {
  const list = document.getElementById("settlist");
  const servers = [];
  let skippedEnv = 0;
  for (const row of list.querySelectorAll(".sett-row")) {
    const origin = row.dataset.origin || "config";
    const name = row.querySelector(".sett-name").value.trim();
    const url = row.querySelector(".sett-url").value.trim();
    if (!name) continue;
    if (origin === "env") {
      // Unveraenderte Env-Zeile: NICHT in die Config importieren
      const original = row.dataset.original || "";
      if (original === name + "|" + url) { skippedEnv++; continue; }
    }
    servers.push({
      name,
      url: (url === "" || url.toLowerCase() === "local") ? null : url,
    });
  }
  const status = document.getElementById("sett-status");
  if (!servers.length && !skippedEnv) {
    settingsStatus = "No valid servers (name required).";
    status.textContent = settingsStatus;
    return;
  }
  // Leere Liste ist ok, wenn env-Zeilen vorhanden sind: Config leeren,
  // die env-Server bleiben aktiv
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ servers }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok) {
      settingsStatus = servers.length
        ? "✅ Saved (" + servers.length + " servers) → " + (d.path || "servers.yaml")
        : "✅ Config cleared — dashboard falls back to the local server.";
      settingsError = "";
      loadSettings();
    } else if (r.status === 409 && d.hint) {
      settingsStatus = "";
      settingsError = d.hint;
      renderSettings();
    } else {
      settingsStatus = "Error: " + (d.error || r.status);
      status.textContent = settingsStatus;
    }
  } catch (e) {
    settingsStatus = "Network error.";
    status.textContent = settingsStatus;
  }
}

/* Prozess-History einmalig vom Server laden (deckt die vollen 300 s ab,
 * damit der Hover-Tooltip auch fuer aeltere Punkte Snapshots hat) */
async function loadProchistory() {
  try {
    const ph = await (await fetch("/api/prochistory")).json();
    for (const [server, snaps] of Object.entries(ph || {})) {
      if (snaps && snaps.length) state.procHistory[server] = snaps;
    }
  } catch (e) { /* Dashboard-Polls fangen das ab */ }
}

refresh();
loadProchistory();
loadStorage();
setInterval(refresh, 1000);
setInterval(() => {
  // Storage-Daten alle 60 s aktualisieren (Füllstände ändern sich langsam)
  if (Date.now() - lastStorageLoad > 60000) { lastStorageLoad = Date.now(); loadStorage(); }
}, 60000);
