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

document.getElementById("net-eq").addEventListener("click", () => {
  state.equalScale = !state.equalScale;
  document.getElementById("net-eq").classList.toggle("active", state.equalScale);
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
  renderStorage();   // Storage-Karten + Dateibrowser respektieren den Filter ebenfalls
  updateMemChart(state.lastMem, state.servers.map(n => ({ name: n })));  // RAM-Graph folgt den Häkchen
  updateCpuChart(state.lastCpu, state.servers.map(n => ({ name: n })));   // CPU-Graph folgt den Häkchen
  applyTermVisibility();  // Terminal-Karten folgen ebenfalls (wenn Tab schon geladen)
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
let storageKeys = [];    // alle bekannten Keys (für globale Modus-Umschaltung)
let storageScale = "full";  // "full" (0-100%) | "zoom" (Messbereich)
let storageOrder = [];   // Karten-Reihenfolge (Drag & Drop, localStorage)
try { storageOrder = JSON.parse(localStorage.getItem("netspy.storageOrder") || "[]"); } catch (e) { storageOrder = []; }
try {
  const sm = JSON.parse(localStorage.getItem("netspy.storageMode") || "{}");
  if (sm && typeof sm === "object") storageMode = sm;
} catch (e) { /* still */ }
try {
  const ss = localStorage.getItem("netspy.storageScale");
  if (ss === "full" || ss === "zoom") storageScale = ss;
} catch (e) { /* still */ }

function stPct(size, used) { return size > 0 ? (used / size) * 100 : 0; }

function fmtBytes(b) {
  if (b == null || b < 0) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : 1) + " " + u[i];
}

// Tooltip-Größen: TB oder GB je nach Größe, 4 Nachkommastellen für Genauigkeit
function fmtBig(b) {
  if (b == null || b < 0) return "–";
  if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(4) + " TB";
  if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(4) + " GB";
  return (b / 1024 ** 2).toFixed(2) + " MB";
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
  storageKeys = allKeys;
  const enabledKeys = allKeys.filter(k => (enabled || []).includes(k));
  // Gespeicherte Drag&Drop-Reihenfolge anwenden (unbekannte Keys ans Ende)
  const orderedKeys = [...enabledKeys].sort((a, b) => {
    const ia = storageOrder.indexOf(a), ib = storageOrder.indexOf(b);
    return (ia === -1 ? 9999 : ia) - (ib === -1 ? 9999 : ib);
  });
  const noHost = !allKeys.length && host_access && Object.values(host_access).some(v => v === false);
  // Server-Filter (Häkchen oben) auch hier anwenden: Server-Prefix im Key
  const visibleKeys = orderedKeys.filter(k => state.visible[k.split(":")[0]] !== false);
  // --- Karten oben: NUR aktivierte (recording) Laufwerke ---
  Object.values(storageCharts).forEach(ch => { try { ch.destroy(); } catch (e) { /* still */ } });
  storageCharts = {};
  if (!enabledKeys.length) {
    grid.innerHTML = noHost
      ? `<p class="hint" style="color:#fbbf24">⚠️ <b>No host access</b> — the container cannot read the host mounts. It must run with <code>--pid=host</code> (host PID namespace): in Unraid go to <b>Docker → NetSpy → Edit → Apply</b> (or <b>Reinstall</b>) so the change takes effect.</p>`
      : `<p class="hint">No drives recording — activate one in the list below.</p>`;
  } else if (!visibleKeys.length) {
    grid.innerHTML = `<p class="hint">All servers hidden — tick a server chip above to show its charts.</p>`;
  } else {
    grid.innerHTML = visibleKeys.map(key => {
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
      return `<div class="stcard${gone ? " gone" : ""}" data-key="${skey}" draggable="true">
        <div class="sthead">
          <span class="stdrag" aria-hidden="true" title="">⠿</span>
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
      // X-Achsen-Label je Modus: 24 h -> nur Uhrzeit, 7 d -> Tag 1-31, 12 m -> Monat 1-12
      const fmtL = t => {
        const d = new Date(t);
        if (serie === "h24") return d.toLocaleString([], { hour: "2-digit", minute: "2-digit", hour12: false });
        if (serie === "d7") return d.toLocaleString([], { day: "numeric" });
        return d.toLocaleString([], { month: "numeric" });
      };
      const labels = points.map(pp => fmtL(pp[0] * 1000));
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
            tooltip: { mode: "index", intersect: false, callbacks: { label: ctx => {
              const pct = ctx.parsed.y;
              return `${fmtBig(size * pct / 100)} / ${fmtBig(size)}  (${pct.toFixed(1)} %)`;
            } } } },
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
      try { localStorage.setItem("netspy.storageMode", JSON.stringify(storageMode)); } catch (err) { /* still */ }
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
  // NUR Server, die im Filter (Häkchen oben) sichtbar sind
  const srvNames = [...new Set(allKeys.map(k => k.includes(":") ? k.split(":")[0] : "(unknown)"))]
    .filter(srv => state.visible[srv] !== false).sort();
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
  // Globale Zeitbereichs-Buttons oben synchronisieren: aktiv, wenn ALLE Karten
  // denselben Modus haben; sonst keiner (Karten wurden einzeln umgestellt)
  const uniModes = new Set(storageKeys.map(k => storageMode[k] || "h24"));
  const uni = uniModes.size === 1 ? [...uniModes][0] : null;
  [["h24", "stmode-24"], ["d7", "stmode-7"], ["m", "stmode-m"]].forEach(([m, id]) => {
    const b = document.getElementById(id);
    if (b) b.classList.toggle("active", uni === m);
  });
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
      `<b style="color:#a78bfa">RAM ${fmtBytes(h.mem_used || 0)} / ${fmtBytes(h.mem_total || 0)} (${memPct}%)</b></span>`;
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
      `<td class="num mem">${h.mem == null ? "–" : fmtBytes(h.mem)}</td></tr>`;
  }).join("");
}

/* ---------- CPU/RAM-Verlauf: ALLE Server in EINEM Chart ---------- */
let memChart = null;
let cpuChart = null;
const MEM_COLORS = ["#22d3ee", "#f59e0b", "#a78bfa", "#4ade80", "#f87171", "#60a5fa", "#f472b6", "#34d399"];
const srvColor = i => MEM_COLORS[i % MEM_COLORS.length];

// Gemeinsame x-Labels (ROHE ts, sortiert) über alle Server einer Serie.
// Achtung: pro Server unterschiedliche Poll-ts -> Union nutzen und Werte
// per ts-Lookup zuordnen, sonst enden Linien bei 1/3 der Breite.
function seriesLabels(ser, servers) {
  const set = new Set();
  (servers || []).forEach(s => { const m = (ser || {})[s.name]; if (m && m.ts) m.ts.forEach(t => set.add(t)); });
  return [...set].sort((a, b) => a - b);
}

function updateMemChart(mem, servers) {
  const el = document.getElementById("memchart");
  if (!el) return;
  const rawLabels = seriesLabels(mem, servers);
  const labelArr = rawLabels.map(fmtTs);
  const datasets = [];
  let maxTotal = 1;
  (servers || []).forEach((s, i) => {
    const m = (mem || {})[s.name];
    if (!m || !m.ts || !m.ts.length) return;
    const color = srvColor(i);
    const totalGB = (m.total || 1) / 1024 ** 3;
    maxTotal = Math.max(maxTotal, totalGB);
    const hidden = state.visible[s.name] === false;
    const tsIdx = new Map(m.ts.map((t, j) => [t, j]));
    // Reale Linie (GB used) — über ALLE x-Positionen, ts-gemappt
    datasets.push({
      label: s.name,
      data: rawLabels.map(t => {
        const j = tsIdx.get(t);
        return j == null ? null : +(((m.used || [])[j] || 0) / 1024 ** 3).toFixed(3);
      }),
      borderColor: color, backgroundColor: color + "22",
      fill: false, pointRadius: 0, spanGaps: true, tension: .25, borderWidth: 2,
      hidden, _totalGB: totalGB,
    });
    // Referenz: installierter RAM (100%) — gestrichelte Linie in Serverfarbe,
    // mit Punkt am linken Rand (damit man sieht, wo 100% liegt)
    datasets.push({
      label: s.name + " — installed (100%)",
      data: labelArr.map((t, j) => +(totalGB).toFixed(3)),
      borderColor: color, borderDash: [6, 4], borderWidth: 1,
      backgroundColor: color,
      fill: false, pointRadius: labelArr.map((t, j) => (j === 0 ? 4 : 0)),
      pointHoverRadius: 4, hidden, _totalGB: totalGB, _ref: true,
    });
  });
  const yMax = Math.ceil(maxTotal * 1.1 / 8) * 8;   // Luft über der höchsten 100%-Linie
  if (!memChart) {
    memChart = new Chart(el, {
      type: "line",
      data: { labels: labelArr, datasets },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(148,163,184,.8)", boxWidth: 12, font: { size: 10 } } },
          tooltip: {
            mode: "index", intersect: false,
            callbacks: {
              label: ctx => {
                const gb = ctx.parsed.y, tot = ctx.dataset._totalGB || 1;
                if (ctx.dataset._ref) return ` ${ctx.dataset.label}: ${gb.toFixed(2)} GB (100 %)`;
                return ` ${ctx.dataset.label}: ${gb.toFixed(2)} GB (${((gb / tot) * 100).toFixed(1)} %)`;
              },
            },
          },
        },
        scales: {
          x: { ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 6, maxRotation: 0, font: { size: 10 } }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { min: 0, max: yMax, ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 5, font: { size: 10 }, callback: v => v + " GB" }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
      },
    });
  } else {
    memChart.data.labels = labelArr;
    memChart.data.datasets = datasets;
    memChart.options.scales.y.max = yMax;
    memChart.update("none");
  }
}

function updateCpuChart(cpu, servers) {
  const el = document.getElementById("cpuchart");
  if (!el) return;
  const rawLabels = seriesLabels(cpu, servers);
  const labelArr = rawLabels.map(fmtTs);
  const datasets = [];
  (servers || []).forEach((s, i) => {
    const m = (cpu || {})[s.name];
    if (!m || !m.ts || !m.ts.length) return;
    const color = srvColor(i);
    const hidden = state.visible[s.name] === false;
    const tsIdx = new Map(m.ts.map((t, j) => [t, j]));
    datasets.push({
      label: s.name,
      data: rawLabels.map(t => {
        const j = tsIdx.get(t);
        return j == null ? null : +(((m.cpu || [])[j] ?? 0)).toFixed(1);
      }),
      borderColor: color, backgroundColor: color + "22",
      fill: false, pointRadius: 0, spanGaps: true, tension: .25, borderWidth: 2,
      hidden,
    });
  });
  if (!cpuChart) {
    cpuChart = new Chart(el, {
      type: "line",
      data: { labels: labelArr, datasets },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: "rgba(148,163,184,.8)", boxWidth: 12, font: { size: 10 } } },
          tooltip: {
            mode: "index", intersect: false,
            callbacks: {
              label: ctx => {
                if (ctx.dataset._ref) return ` ${ctx.dataset.label}`;
                return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} %`;
              },
            },
          },
        },
        scales: {
          x: { ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 6, maxRotation: 0, font: { size: 10 } }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { min: 0, max: 110, ticks: { color: "rgba(148,163,184,.5)", maxTicksLimit: 5, font: { size: 10 }, callback: v => v + "%" }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
      },
    });
  } else {
    cpuChart.data.labels = labelArr;
    cpuChart.data.datasets = datasets;
    cpuChart.update("none");
  }
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
  state.lastMem = d.mem || {};
  state.lastCpu = d.cpu || {};
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
  updateLatency(d.latency);
  updateMemChart(d.mem, d.servers);
  updateCpuChart(d.cpu, d.servers);
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
  try { localStorage.setItem("netspy.storageScale", mode); } catch (e) { /* still */ }
  const f = document.getElementById("stscale-full"), z = document.getElementById("stscale-zoom");
  if (f) f.classList.toggle("active", mode === "full");
  if (z) z.classList.toggle("active", mode === "zoom");
  renderStorage();
}
document.getElementById("stscale-full").addEventListener("click", () => setStorageScale("full"));
document.getElementById("stscale-zoom").addEventListener("click", () => setStorageScale("zoom"));
/* Globale Zeitbereich-Umschaltung: setzt den Modus für ALLE Karten gleichzeitig */
function setStorageModeAll(mode) {
  for (const k of storageKeys) storageMode[k] = mode;
  try { localStorage.setItem("netspy.storageMode", JSON.stringify(storageMode)); } catch (e) { /* still */ }
  renderStorage();
}
document.getElementById("stmode-24").addEventListener("click", () => setStorageModeAll("h24"));
document.getElementById("stmode-7").addEventListener("click", () => setStorageModeAll("d7"));
document.getElementById("stmode-m").addEventListener("click", () => setStorageModeAll("m"));
/* Gespeicherte Skala beim Laden auf die Buttons uebertragen */
if (storageScale === "zoom") {
  const f = document.getElementById("stscale-full"), z = document.getElementById("stscale-zoom");
  if (f) f.classList.remove("active");
  if (z) z.classList.add("active");
}

/* Tab-Umschaltung: Network / Disk / CPU-RAM / Storage / Settings */
document.getElementById("tabbtn-net").addEventListener("click", () => setTab("net"));
document.getElementById("tabbtn-disk").addEventListener("click", () => setTab("disk"));
document.getElementById("tabbtn-sys").addEventListener("click", () => setTab("sys"));
document.getElementById("tabbtn-storage").addEventListener("click", () => setTab("storage"));
document.getElementById("tabbtn-term").addEventListener("click", () => setTab("term"));
document.getElementById("tabbtn-settings").addEventListener("click", () => setTab("settings"));

const TAB_IDS = ["net", "disk", "sys", "storage", "term", "settings"];
function setTab(which) {
  try { localStorage.setItem("netspy.tab", which); } catch (e) { /* still */ }
  for (const t of TAB_IDS) {
    document.getElementById("panel-" + t).classList.toggle("hidden", t !== which);
    document.getElementById("tabbtn-" + t).classList.toggle("active", t === which);
    document.getElementById("tabbtn-" + t).setAttribute("aria-selected", t === which ? "true" : "false");
  }
  if (which === "settings") loadSettings();
  if (which === "term") loadTerminal();
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
  await loadTermState();   // Terminal-Ziele (für die Settings-Sektion)
  renderSettings();
  renderTermSettings();
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

/* ================= 🖥️ SSH Terminal (ttyd) ================= */
let termState = { data: null, lastKey: "", editor: [], _editorSync: "" };
let termOrder = [];   // Terminal-Karten-Reihenfolge (Drag & Drop, localStorage)
let termHeights = {}; // Terminal-Höhen je Ziel (Resize, localStorage)
try { termOrder = JSON.parse(localStorage.getItem("netspy.termOrder") || "[]"); } catch (e) { termOrder = []; }
try { termHeights = JSON.parse(localStorage.getItem("netspy.termHeights") || "{}"); } catch (e) { termHeights = {}; }
const TERM_DEFAULT_H = 340; // Standard-/Reset-Höhe eines Terminal-Frames

/* ---------- Linux-Befehlsliste (Cheat-Sheet neben den Konsolen) ---------- */
const CMD_GROUPS = [
  { title: "Filesystems & mounts", items: [
    { c: "mount -a", d: "Mount all filesystems from /etc/fstab — run it after editing fstab or when a boot skipped mounts." },
    { c: "mount -o remount,rw /", d: "Remount the root filesystem read-write — the classic fix when a disk error switched it to read-only." },
    { c: "findmnt", d: "Show the mount tree: which device is mounted where and with which options." },
    { c: "fstrim -av", d: "Trim all mounted SSDs (discard unused blocks). Keeps flash storage fast; safe on modern systems." },
    { c: "df -h", d: "Show disk usage of all mounted filesystems, human-readable sizes." },
    { c: "lsblk", d: "List block devices: drives, partitions and their mount points." },
    { c: "du -sh *", d: "Show the total size of every file/folder in the current directory." },
  ]},
  { title: "Packages (APT — Debian/Ubuntu)", items: [
    { c: "apt-get update && apt-get upgrade -y", d: "The daily routine in one copy: refresh the package index and install all upgrades right away." },
    { c: "apt-get update", d: "Refresh the package index from the configured repositories." },
    { c: "apt list --upgradable", d: "List the packages that have an available upgrade — look before you upgrade." },
    { c: "apt-get upgrade -y", d: "Install all available upgrades of installed packages (-y: no prompt). Keeps installed/removed set unchanged." },
    { c: "apt-get dist-upgrade -y", d: "Like upgrade, but may also install/remove packages when dependencies demand it." },
    { c: "apt-get install <package>", d: "Install a package. Replace <package> with the real name (e.g. apt-get install tmux)." },
    { c: "apt-get purge <package>", d: "Remove a package INCLUDING its config files. Replace <package> with the package name." },
    { c: "apt-get autoremove --purge -y", d: "Remove packages that are no longer needed, incl. their config files." },
    { c: "apt-get clean", d: "Delete cached .deb files from /var/cache/apt to free disk space." },
  ]},
  { title: "Docker", items: [
    { c: "docker ps -a", d: "List ALL containers incl. stopped ones — see what exists before you prune." },
    { c: "docker stats --no-stream", d: "Live CPU/RAM/network usage of running containers (one snapshot, no scrolling)." },
    { c: "docker images", d: "List local images with their sizes and tags." },
    { c: "docker logs -f <container>", d: "Follow the log output of a container live. Replace <container> with its name (docker ps -a shows it)." },
    { c: "docker restart <container>", d: "Restart a container, e.g. after a config change. Replace <container> with its name." },
    { c: "docker system df", d: "Disk-usage overview: how much images, containers, volumes and build cache occupy. No flags — run it first to see where space went." },
    { c: "docker container prune -f", d: "Remove ALL stopped containers. Flag -f/--force: skip the confirmation prompt (otherwise docker asks before deleting)." },
    { c: "docker image prune -a -f", d: "Remove images no container uses. Flags: -f skip the prompt; -a/--all remove ALL unused images. WITHOUT -a only untagged 'dangling' images are removed — the gentle choice is 'docker image prune -f', the thorough one is with -a." },
    { c: "docker volume prune -f", d: "Remove volumes not referenced by any container. Flag -f: skip the prompt. ⚠️ Volumes are where containers keep data — only run when you know no container needs them." },
    { c: "docker network prune -f", d: "Remove custom networks not used by any container. Flag -f: skip the prompt." },
    { c: "docker builder prune -f", d: "Clear the Docker build cache (often the biggest hidden space eater after image updates). Flag -f: skip the prompt." },
    { c: "docker system prune -f", d: "Classic cleanup: stopped containers, unused networks, dangling images + build cache. Flag -f: skip the prompt. Safer than -a because it keeps ALL tagged images." },
    { c: "docker system prune -a -f", d: "Everything prune -f does, PLUS all images no container uses. Flags: -a/--all is what removes the old tagged images, -f skips the prompt. Run 'docker system df' first to compare." },
    { c: "docker system prune -a -f --volumes", d: "Maximum cleanup: like -a -f, and --volumes additionally removes unused volumes. ⚠️ Volumes can hold data — only run when you are sure nothing needs them." },
  ]},
  { title: "Unraid", items: [
    { c: "mover start", d: "Start the mover: moves files from the cache pool to the array. Run it when the cache is filling up." },
    { c: "mover stop", d: "Stop a running mover (e.g. before a parity check or when it blocks an app)." },
    { c: "mover status", d: "Show the mover state (running/idle) and when it last ran. Note: only newer Unraid versions know this subcommand." },
    { c: "mdcmd status", d: "Show the array status: which disks are spun up, parity state, array health (mdcmd is Unraid's array tool)." },
    { c: "tail -50 /var/log/syslog", d: "Show the newest Unraid system messages — the first stop when a disk, share or plugin acts up." },
    { c: "ls -lh /boot/logs", d: "List the diagnostics archives on the flash drive (Tools → Diagnostics writes them here) — handy to find the newest one." },
    { c: "nvidia-smi", d: "GPU status: model, driver, temperature and current VRAM usage (only with an NVIDIA GPU)." },
  ]},
  { title: "TrueNAS", items: [
    { c: "zpool status", d: "Pool health: state of every vdev, last scrub and any read/write/checksum errors. Run this first when something feels wrong." },
    { c: "zpool status -v", d: "Same as above, plus the list of files with persistent errors (checksum/read/write)." },
    { c: "zpool clear <pool>", d: "RESET the error counters (read/write/checksum) of a pool — the classic 'clear array errors' after a failed disk was fixed or a scrub ran clean. The pool then leaves its DEGRADED state if the device reports no more errors. Per device: zpool clear <pool> <device>." },
    { c: "zpool online <pool> <device>", d: "Bring a disk back ONLINE after it was fixed/replaced (e.g. after zpool offline). Replace <device> with the disk (zpool status shows it)." },
    { c: "zpool replace <pool> <old> <new>", d: "Replace a faulty disk with a new one and let ZFS rebuild (resilver) the data. Replace <old> and <new> with the disk IDs from zpool status." },
    { c: "zpool list", d: "Overview of all pools: size, used space, fragmentation and the current status." },
    { c: "zfs list", d: "List all datasets with used space, quotas and compression ratio." },
    { c: "zfs list -t snapshot", d: "List all snapshots and how much space each one occupies." },
    { c: "zpool scrub <pool>", d: "Start a data-integrity scrub on a pool. Replace <pool> with the pool name (zpool list shows it). ⚠️ Causes heavy I/O while running." },
    { c: "zfs snapshot <dataset>@manual", d: "Take a manual snapshot before risky changes. Replace <dataset> with e.g. TrueNAS/data — snapshots are instant and cheap." },
    { c: "smbstatus", d: "Show active SMB connections and open files — who is currently accessing the shares." },
  ]},
  { title: "System, services & processes", items: [
    { c: "systemctl status", d: "Overview of the systemd state and the most important services." },
    { c: "systemctl restart <service>", d: "Restart a systemd service (e.g. nginx, ssh, docker). Replace <service> with its name." },
    { c: "systemctl enable --now <service>", d: "Start a service now AND make it auto-start on boot. Replace <service> with its name." },
    { c: "journalctl -xe", d: "Show recent systemd logs; -x explains the entries, -e jumps to the newest messages." },
    { c: "journalctl -u <service> -f", d: "Follow the log of ONE service live — the fastest way to see why it fails. Replace <service>." },
    { c: "dmesg | tail -20", d: "Show the newest kernel messages: hardware errors, disk problems, USB events." },
    { c: "ps aux --sort=-%cpu | head -15", d: "The 15 processes using the most CPU right now." },
    { c: "free -h", d: "Show RAM + swap usage, human-readable." },
    { c: "htop", d: "Interactive process viewer with CPU/RAM bars (install first: apt-get install htop)." },
    { c: "uptime", d: "How long the system has been running plus the current load average." },
  ]},
  { title: "Network", items: [
    { c: "ip a", d: "Show all network interfaces and their IP addresses." },
    { c: "ip route show", d: "Show the routing table — which gateway leads where (the default route is the uplink)." },
    { c: "ss -tulpn", d: "List listening/established TCP+UDP sockets with the owning process (-p needs root)." },
    { c: "ping -c 4 <host>", d: "Check connectivity to a host: 4 pings with timing. Replace <host> with an IP or name." },
    { c: "curl -I <url>", d: "Fetch only the HTTP headers of a URL — quick check whether a web service answers (HTTP 200 = ok)." },
  ]},
];

let cmdOpen = true;   // Befehlsliste sichtbar?
try { cmdOpen = localStorage.getItem("netspy.cmdPanel") !== "0"; } catch (e) { /* still */ }

let cmdFavs = [];     // favorisierte Befehle (in Klick-Reihenfolge)
try { cmdFavs = JSON.parse(localStorage.getItem("netspy.cmdFavs") || "[]"); } catch (e) { cmdFavs = []; }
// Befehl -> Beschreibung, fuer die Favoriten-Gruppe (ohne doppelte Daten)
const CMD_INDEX = {};
CMD_GROUPS.forEach(g => g.items.forEach(it => { CMD_INDEX[it.c] = it; }));

function toggleFav(c) {
  const i = cmdFavs.indexOf(c);
  if (i >= 0) cmdFavs.splice(i, 1);
  else cmdFavs.push(c);
  try { localStorage.setItem("netspy.cmdFavs", JSON.stringify(cmdFavs)); } catch (e) { /* still */ }
  renderCmdList();
}

function renderCmdList() {
  const box = document.getElementById("cmdlist");
  if (!box) return;
  const rowHtml = (it, fav) =>
    `<div class="cmditem" data-desc="${esc(it.d)}">` +
    `<button class="cmdfav ${fav ? "on" : ""}" title="${fav ? "remove from favorites" : "add to favorites"}">${fav ? "★" : "☆"}</button>` +
    `<code>${esc(it.c)}</code>` +
    `<button class="cmdcopy" title="copy to clipboard">⧉</button></div>`;
  const groups = [];
  if (cmdFavs.length) {
    const favItems = cmdFavs.filter(c => CMD_INDEX[c]).map(c => CMD_INDEX[c]);
    groups.push(`<div class="cmdgroup"><div class="cmdgtitle">⭐ Favorites</div>` +
      favItems.map(it => rowHtml(it, true)).join("") + `</div>`);
  }
  CMD_GROUPS.forEach(g => {
    groups.push(`<div class="cmdgroup"><div class="cmdgtitle">${esc(g.title)}</div>` +
      g.items.map(it => rowHtml(it, cmdFavs.includes(it.c))).join("") + `</div>`);
  });
  box.innerHTML = groups.join("");
  // Copy + Favoriten + Tooltip je Zeile binden
  box.querySelectorAll(".cmditem").forEach(row => {
    const code = row.querySelector("code").textContent;
    const copyBtn = () => {
      copyText(code).then(ok => {
        const btn = row.querySelector(".cmdcopy");
        if (!btn) return;
        const old = btn.textContent;
        btn.textContent = ok ? "✓" : "✗";
        btn.classList.toggle("copied", ok);
        setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1300);
      });
    };
    row.querySelector(".cmdcopy").addEventListener("click", copyBtn);
    row.querySelector("code").addEventListener("click", copyBtn);
    row.querySelector(".cmdfav").addEventListener("click", () => toggleFav(code));
  });
}

/* Hover-Beschreibung mit 1-s-Delay: erst wenn die Maus eine Zeile lang
   ruht, poppt der Tooltip auf. Verlassen der Zeile/Liste, Scrollen (Liste
   oder Seite), Fenster-Blur/Tab-Wechsel verstecken ihn SOFORT und brechen
   den Timer — beim schnellen Durchscrollen poppt nichts auf, und ein
   verlorenes mouseleave kann den Tooltip nicht mehr einfrieren: ein
   document-weiter mousemove-Tracker (elementFromPoint) versteckt ihn bei
   JEDER Bewegung ausserhalb einer Zeile. */
let tipTimer = null;
let tipRowEl = null;
let tipX = -1, tipY = -1;   // letzte bekannte Mausposition (Watchdog)
let tipLastMove = 0;        // Zeitstempel der letzten Mausbewegung
const TIP_DELAY = 1000;
const TIP_STALE = 5000;     // ohne Mausbewegung blendet sich der Tooltip selbst aus

function hideCmdTip() {
  const tip = document.getElementById("cmdtiptip");
  if (tip) tip.classList.add("hidden");
}

function cancelTip() {
  if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
  tipRowEl = null;
}

function positionTip(x, y) {
  const tip = document.getElementById("cmdtiptip");
  if (!tip) return;
  const pad = 14;
  let px = x + pad, py = y + pad;
  const r = tip.getBoundingClientRect();
  if (px + r.width > window.innerWidth - 8) px = x - r.width - pad;
  if (py + r.height > window.innerHeight - 8) py = y - r.height - pad;
  tip.style.left = px + "px";
  tip.style.top = py + "px";
}

/* Zeile unter dem Cursor (auch ueber iframes hinweg — elementFromPoint
   liefert dort das iframe-Element, closest findet keine .cmditem). */
function tipRowFrom(e) {
  const el = (document.elementFromPoint && e.clientX != null)
    ? document.elementFromPoint(e.clientX, e.clientY) : e.target;
  return el && el.closest ? el.closest(".cmditem") : null;
}

function armTip(row, x, y) {
  if (tipRowEl === row && tipTimer) return;   // Timer fuer diese Zeile laeuft schon
  cancelTip();
  hideCmdTip();   // Zeilenwechsel: alter Tooltip sofort weg, neuer erst nach Delay
  tipRowEl = row;
  tipTimer = setTimeout(() => {
    tipTimer = null;
    const tip = document.getElementById("cmdtiptip");
    if (!tip || !tipRowEl || !tipRowEl.isConnected) return;
    tip.textContent = tipRowEl.dataset.desc || "";
    tip.classList.remove("hidden");
    positionTip(x, y);
  }, TIP_DELAY);
}

function bindCmdTip() {
  const box = document.getElementById("cmdlist");
  if (!box) return;
  box.addEventListener("mouseover", e => {
    const row = e.target && e.target.closest ? e.target.closest(".cmditem") : null;
    if (row) armTip(row, e.clientX, e.clientY);
  });
  // Sichtbarer Tooltip folgt der Maus nur innerhalb der Zeile
  box.addEventListener("mousemove", e => {
    if (tipTimer === null) {
      const tip = document.getElementById("cmdtiptip");
      if (tip && !tip.classList.contains("hidden") && tipRowEl) positionTip(e.clientX, e.clientY);
    }
  });
  // Jede Mausbewegung im Dokument: ausserhalb einer Zeile -> sofort weg
  document.addEventListener("mousemove", e => {
    tipX = e.clientX; tipY = e.clientY;
    tipLastMove = Date.now();
    const row = tipRowFrom(e);
    if (!row) { cancelTip(); hideCmdTip(); }
    else if (row !== tipRowEl) armTip(row, e.clientX, e.clientY);
  }, { passive: true });
  box.addEventListener("mouseleave", () => { cancelTip(); hideCmdTip(); });
  // Scrollen verdeckt Inhalte -> Tooltip sofort zu + Timer weg
  box.addEventListener("scroll", () => { cancelTip(); hideCmdTip(); }, { passive: true });
  window.addEventListener("scroll", () => { cancelTip(); hideCmdTip(); }, { passive: true });
  window.addEventListener("blur", () => { cancelTip(); hideCmdTip(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelTip(); hideCmdTip(); }
  });
  // Maus verlaesst das FENSTER: das letzte mouseout hat relatedTarget=null.
  // (zuverlaessiger als mouseleave auf document; auch bei schnellen Uebergaengen)
  document.addEventListener("mouseout", e => {
    if (!e.relatedTarget) { cancelTip(); hideCmdTip(); }
  });
  /* WATCHDOG (Strategie: nicht auf Maus-Events verlassen): solange der
     Tooltip sichtbar ist oder ein Timer laeuft, prueft ein 200-ms-Intervall,
     ob die Maus noch ueber der Ziel-Zeile liegt (elementFromPoint an der
     letzten bekannten Mausposition). Damit werden AUCH Faelle ohne
     Mausbewegung erkannt: Scrollen unter statischem Cursor (die Zeile
     wandert unter der Maus weg) oder verlorene Events — der Tooltip
     verschwindet dann spätestens nach ~200 ms.
     ZUSAETZLICH Lebensdauer: Wenn die Maus das FENSTER verlaesst (noVNC/
     VNC: die Seite bekommt danach gar keine Events mehr, die letzte
     Position bleibt auf der Zeile stehen), blendet sich der Tooltip nach
     TIP_STALE ms ohne jede Mausbewegung von selbst aus — er kann damit
     prinzipiell nicht mehr dauerhaft kleben. Minimale Mausbewegung auf der
     Zeile (z. B. beim Lesen) haelt ihn frisch. */
  setInterval(() => {
    const tip = document.getElementById("cmdtiptip");
    const active = tip && (!tip.classList.contains("hidden") || tipTimer);
    if (!active || tipX < 0) return;
    const el = document.elementFromPoint(tipX, tipY);
    const row = el && el.closest ? el.closest(".cmditem") : null;
    if (!row || row !== tipRowEl) { cancelTip(); hideCmdTip(); }
    else if (!tip.classList.contains("hidden") && Date.now() - tipLastMove > TIP_STALE) {
      cancelTip(); hideCmdTip();
    }
  }, 200);
}

async function copyText(t) {
  // navigator.clipboard braucht einen secure context (https/localhost);
  // im LAN (http://192.168.x.x) fallback auf execCommand.
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(t);
      return true;
    }
  } catch (e) { /* fall through */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e2) { return false; }
}

/* Befehlsliste ein-/ausblenden (Zustand in localStorage) */
function applyCmdPanel() {
  const panel = document.getElementById("cmdpanel");
  const tip = document.getElementById("cmdtiptip");
  if (!panel) return;
  panel.style.display = cmdOpen ? "" : "none";
  if (!cmdOpen && tip) tip.classList.add("hidden");
}

function initCmdPanel() {
  renderCmdList();
  bindCmdTip();
  applyCmdPanel();
  const close = document.getElementById("cmdclose");
  if (close) close.addEventListener("click", () => {
    cmdOpen = false;
    try { localStorage.setItem("netspy.cmdPanel", "0"); } catch (e) { /* still */ }
    applyCmdPanel();
  });
  // 📋-Button (wird je Terminal-Tab-Zustand neu gerendert): Toggle per Delegation
  document.addEventListener("click", e => {
    const t = e.target && e.target.closest ? e.target.closest("#cmdopen") : null;
    if (!t) return;
    cmdOpen = !cmdOpen;
    try { localStorage.setItem("netspy.cmdPanel", cmdOpen ? "1" : "0"); } catch (err) { /* still */ }
    applyCmdPanel();
  });
}

const CMDTOGGLE = `<button id="cmdopen" class="chip-btn" title="Show or hide the Linux command list next to the terminals">📋 commands</button> `;

initCmdPanel();

async function loadTermState() {
  try {
    const r = await fetch("/api/terminal");
    termState.data = await r.json();
  } catch (e) {
    termState.data = null;
  }
}

function termKey() {
  const d = termState.data;
  if (!d) return "";
  return JSON.stringify((d.targets || []).map(t => [t.name, t.port, t.host, t.user]));
}

/* Terminal-Tab-Login (TTYD_USER/TTYD_PASS): die iframes erscheinen erst nach
   erfolgreichem Login. Login-Status nur in sessionStorage (ueberlebt Reload
   im selben Tab, nicht das Schliessen des Browsers). */
function termLoggedIn() {
  try { return sessionStorage.getItem("netspy.termLogin") === "1"; } catch (e) { return false; }
}

function termLoginForm() {
  return `<div class="termlogin">
    <div style="font-size:15px;font-weight:600;margin-bottom:4px">🔒 SSH Terminal login</div>
    <p class="hint">The terminals are unlocked with the <b>container environment variables</b> <code>TTYD_USER</code> / <code>TTYD_PASS</code> — set on the NetSpy <b>container</b> (Unraid: Docker → NetSpy → <b>Edit</b> → Apply → Recreate), not in a NetSpy settings page. The dashboard (charts, settings) stays open either way.</p>
    <p class="hint">Don't know the values? Check them under Docker → NetSpy → Edit — after changing them the container must be recreated. They are never stored in NetSpy itself.</p>
    <div class="termlogin-row">
      <input id="tlogin-user" placeholder="TTYD_USER" autocomplete="username" spellcheck="false">
      <input id="tlogin-pass" type="password" placeholder="TTYD_PASS" autocomplete="current-password">
      <button id="tlogin-btn" class="chip-btn">Login</button>
    </div>
    <div id="tlogin-msg" class="hint" style="margin-top:6px"></div>
  </div>`;
}

function bindTermLogin() {
  const btn = document.getElementById("tlogin-btn");
  if (!btn) return;
  const doLogin = async () => {
    const u = document.getElementById("tlogin-user");
    const p = document.getElementById("tlogin-pass");
    const msg = document.getElementById("tlogin-msg");
    if (!u.value || !p.value) { msg.textContent = "Enter username and password."; return; }
    msg.textContent = "…";
    try {
      const r = await fetch("/api/terminal/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user: u.value, pass: p.value }),
      });
      if (r.ok) {
        try { sessionStorage.setItem("netspy.termLogin", "1"); } catch (err) { /* still */ }
        termState.lastKey = "";
        loadTerminal();
      } else {
        msg.innerHTML = "❌ Invalid credentials — the login uses the <b>container env vars</b> <code>TTYD_USER</code> / <code>TTYD_PASS</code> (Unraid: Docker → NetSpy → Edit). After changing them, recreate the container.";
        p.value = "";
        p.focus();
      }
    } catch (e) {
      msg.textContent = "❌ Login failed (server unreachable).";
    }
  };
  btn.addEventListener("click", doLogin);
  const u = document.getElementById("tlogin-user");
  const p = document.getElementById("tlogin-pass");
  u.addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
  p.addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
  u.focus();
}

async function loadTerminal() {
  await loadTermState();
  const grid = document.getElementById("termgrid");
  const info = document.getElementById("terminfo");
  const ub = document.getElementById("termuserbar");
  if (!grid) return;
  const d = termState.data;
  if (!d) {
    grid.innerHTML = ""; info.innerHTML = `<p class="hint">Terminal not available.</p>`;
    if (ub) ub.innerHTML = "";
    return;
  }
  // 1) Keine TTYD_USER/TTYD_PASS gesetzt -> Hinweis, was zu tun ist
  if (!d.enabled) {
    termState.lastKey = "";
    grid.innerHTML = "";
    info.innerHTML = "";
    if (ub) ub.innerHTML = CMDTOGGLE +
      `<p class="hint" style="color:#fbbf24">🔒 Terminal login not configured — set <code>TTYD_USER</code> and <code>TTYD_PASS</code> as container environment variables (Unraid: Docker → NetSpy → edit → apply), then recreate the container. Settings stay open; the Terminal tab stays locked until then.</p>`;
    return;
  }
  // 2) Nicht eingeloggt -> Login-Formular
  if (!termLoggedIn()) {
    termState.lastKey = "";
    grid.innerHTML = termLoginForm();
    info.innerHTML = "";
    if (ub) ub.innerHTML = CMDTOGGLE + `<p class="hint">🔒 Terminal tab is locked — log in to open the terminals.</p>`;
    bindTermLogin();
    return;
  }
  // 3) Eingeloggt -> Terminals (Kopfzeile mit Logout + Größen-Reset)
  if (ub) {
    ub.innerHTML = `<span class="hint">🔓 SSH terminals unlocked</span> ` +
      CMDTOGGLE +
      `<button id="tresetsizes" class="chip-btn" title="Reset all terminal window heights to the default">↺ reset sizes</button> ` +
      `<button id="tlogout" class="chip-btn" title="Lock the terminals again">logout</button>`;
    const rs = document.getElementById("tresetsizes");
    if (rs) rs.addEventListener("click", () => {
      termHeights = {};
      try { localStorage.removeItem("netspy.termHeights"); } catch (err) { /* still */ }
      document.querySelectorAll(".termframe").forEach(f => f.style.height = TERM_DEFAULT_H + "px");
    });
    const lo = document.getElementById("tlogout");
    if (lo) lo.addEventListener("click", () => {
      try { sessionStorage.removeItem("netspy.termLogin"); } catch (err) { /* still */ }
      termState.lastKey = "";
      loadTerminal();
    });
  }
  const k = termKey();
  // Konfig unveraendert -> iframes NICHT neu bauen (Verbindungen bleiben aktiv)
  if (k === termState.lastKey && grid.children.length) {
    updateTermStatus();
    return;
  }
  termState.lastKey = k;
  // Reihenfolge: Drag&Drop-Order (unbekannte Ziele ans Ende). suggested-
  // Zeilen (Monitoring-Server ohne User, port 0) bekommen KEINE Karte.
  const targets = (d.targets || []).filter(t => !t.suggested).slice().sort((a, b) => {
    const ia = termOrder.indexOf(a.name), ib = termOrder.indexOf(b.name);
    return (ia === -1 ? 9999 : ia) - (ib === -1 ? 9999 : ib);
  });
  grid.innerHTML = targets.map((t, i) => `
    <div class="termcard" data-name="${esc(t.name)}" draggable="true" style="order:${i}">
      <div class="termhead" title="drag to reorder">
        <span class="termdrag" aria-hidden="true">⠿</span>
        <b>${esc(t.name)}</b>
        <span class="hint">${esc(t.user)}@${esc(t.host)}</span>
        <span class="termstat ${t.running ? "ok" : "bad"}" data-port="${t.port}">${t.running ? "● active" : "○ offline"}</span>
      </div>
      <iframe class="termframe" data-port="${t.port}" src="http://${location.hostname}:${t.port}"
        style="height:${termHeights[t.name] || TERM_DEFAULT_H}px"></iframe>
      <div class="termresize" title="drag to resize"></div>
    </div>`).join("") ||
    `<p class="hint">No terminal targets yet — add them in <b>Settings → 🖥️ SSH Terminal</b>.</p>`;
  bindTermDnD(grid);
  bindTermResize();
  applyTermVisibility();
  updateTermStatus();
}

/* Terminal-Karten folgen dem Server-Filter (Häkchen): nur Karten, deren
   Zielname einem Monitoring-Server entspricht, werden ausgeblendet. Manuelle
   Ziele mit anderem Namen bleiben immer sichtbar. display:none lässt die
   iframes weiterlaufen — SSH-Verbindungen bleiben aktiv. */
function applyTermVisibility() {
  const grid = document.getElementById("termgrid");
  if (!grid) return;
  grid.querySelectorAll(".termcard").forEach(card => {
    const nm = card.dataset.name;
    if (state.servers.includes(nm)) {
      card.style.display = state.visible[nm] === false ? "none" : "";
    }
  });
}

/* Drag & Drop: Terminal-Karten umsortieren. Bewusst OHNE Re-Render —
   per DOM-Move bleiben die iframes geladen und die Verbindungen aktiv.
   Drop-Zone ist der GRID (nicht nur die Karten): die Karten bestehen zu
   ~90 % aus Iframes, und Drag-Events ueber Iframes gehen ans Iframe-
   Dokument (cross-origin) — die Zielposition wird deshalb aus der
   Cursor-Y-Position relativ zu den Karten bestimmt. */
function bindTermDnD(grid) {
  let dragName = null;
  grid.querySelectorAll(".termcard").forEach(card => {
    card.addEventListener("dragstart", e => {
      dragName = card.dataset.name;
      try {
        e.dataTransfer.setData("text/plain", dragName);  // Pflicht fuer echten Drag
        e.dataTransfer.effectAllowed = "move";
      } catch (err) { /* still */ }
      card.style.opacity = ".5";
      grid.classList.add("termdrag-active");
    });
    card.addEventListener("dragend", () => {
      card.style.opacity = "";
      grid.classList.remove("termdrag-active");
      grid.querySelectorAll(".termcard.dragover").forEach(c => c.classList.remove("dragover"));
    });
  });
  // Drop im ganzen Grid erlauben (Karten-Kopfzeile, Raender, Zwischenraeume)
  grid.addEventListener("dragover", e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    // Ziel-Karte unter dem Cursor markieren (nur wenn kein iframe im Weg)
    const t = e.target.closest ? e.target.closest(".termcard") : null;
    grid.querySelectorAll(".termcard.dragover").forEach(c => c.classList.remove("dragover"));
    if (t && t.dataset.name !== dragName) t.classList.add("dragover");
  });
  grid.addEventListener("drop", e => {
    e.preventDefault();
    grid.querySelectorAll(".termcard.dragover").forEach(c => c.classList.remove("dragover"));
    if (!dragName) return;
    const cards = [...grid.querySelectorAll(".termcard")];
    // Anzeige-Reihenfolge (CSS order), nicht DOM-Reihenfolge
    const byOrder = (a, b) => (+(a.style.order || 0)) - (+(b.style.order || 0));
    const ordered = cards.slice().sort(byOrder);
    const others = ordered.filter(c => c.dataset.name !== dragName);
    if (!others.length) return;
    // Einfuegeposition aus der Cursor-Y-Position (vor der Karte, deren
    // obere Haelfte der Cursor passiert hat; sonst ans Ende)
    const y = e.clientY;
    let idx = others.length;
    for (let i = 0; i < others.length; i++) {
      const r = others[i].getBoundingClientRect();
      if (y < r.top + r.height / 2) { idx = i; break; }
    }
    const names = others.map(c => c.dataset.name);
    names.splice(idx, 0, dragName);
    termOrder = names;
    try { localStorage.setItem("netspy.termOrder", JSON.stringify(termOrder)); } catch (err) { /* still */ }
    // NUR order-Werte aendern (KEIN DOM-Move!) — appendChild wuerde die
    // iframes in Chromium neu laden und die SSH-Sessions killen.
    const byName = {};
    cards.forEach(c => { byName[c.dataset.name] = c; });
    names.forEach((n, i) => { byName[n].style.order = i; });
  });
}

/* Resize: Höhe des Terminals per Zug am Balken (150–1000 px, gespeichert).
   Auto-Scroll: erreicht der Cursor beim Ziehen den oberen/unteren
   Fensterrand, scrollt die Seite sanft nach (sonst lässt sich das unterste
   Terminal nicht über den Viewport hinaus vergrößern). Speed steigt mit der
   Eindringtiefe in die Randzone, aber bewusst langsam (2–6 px/Frame). */
function bindTermResize() {
  document.querySelectorAll(".termresize").forEach(h => {
    h.addEventListener("pointerdown", e => {
      e.preventDefault();
      const card = h.closest(".termcard");
      const frame = card.querySelector(".termframe");
      const name = card.dataset.name;
      const startY = e.clientY;
      const startH = frame.getBoundingClientRect().height;
      let lastY = e.clientY, raf = null, active = true;
      try { h.setPointerCapture(e.pointerId); } catch (err) { /* still */ }
      const move = ev => {
        lastY = ev.clientY;
        const nh = Math.max(150, Math.min(1000, startH + (ev.clientY - startY)));
        frame.style.height = nh + "px";
        termHeights[name] = nh;
      };
      // Sanfter Auto-Scroll-Loop: läuft nur während des Zugs. In der
      // Randzone scrollt die Seite UND die Höhe wächst/schrumpft mit —
      // der Resize-Balken bleibt so unter dem Cursor, der User kann die
      // Karte auch dann weiter vergrößern, wenn die Maus am Fensterrand
      // "klebt" (kein weiteres Maus-Delta mehr möglich).
      const tick = () => {
        raf = null;
        if (!active) return;
        const zone = 90, vh = window.innerHeight;
        let dy = 0;
        if (lastY < zone) {
          // obere Randzone -> nach oben scrollen (kleinere Fenster)
          dy = -Math.min(6, 2 + (zone - lastY) / 22);
          window.scrollBy(0, dy);
        } else if (lastY > vh - zone) {
          // untere Randzone -> nach unten scrollen
          dy = Math.min(6, 2 + (lastY - (vh - zone)) / 22);
          window.scrollBy(0, dy);
        }
        if (dy) {
          const cur = parseFloat(frame.style.height) || TERM_DEFAULT_H;
          const nh = Math.max(150, Math.min(1000, cur + dy));
          frame.style.height = nh + "px";
          termHeights[name] = nh;
        }
        raf = requestAnimationFrame(tick);
      };
      const stop = () => {
        active = false;
        if (raf) cancelAnimationFrame(raf);
        raf = null;
        h.removeEventListener("pointermove", move);
        h.removeEventListener("pointerup", stop);
        h.removeEventListener("pointercancel", stop);
        try { localStorage.setItem("netspy.termHeights", JSON.stringify(termHeights)); } catch (err) { /* still */ }
      };
      raf = requestAnimationFrame(tick);
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", stop);
      h.addEventListener("pointercancel", stop);
    });
  });
}

function updateTermStatus() {
  const d = termState.data;
  if (!d) return;
  for (const t of d.targets || []) {
    const s = document.querySelector(`.termstat[data-port="${t.port}"]`);
    if (s) {
      s.textContent = t.running ? "● active" : "○ offline";
      s.className = "termstat " + (t.running ? "ok" : "bad");
    }
  }
  const info = document.getElementById("terminfo");
  if (info) info.innerHTML = d.error
    ? `<p class="hint" style="color:#fbbf24">⚠️ ${esc(d.error)}</p>` : "";
}

/* ---------- Settings: Terminal-Ziel-Editor ---------- */
function syncTermEditor() {
  // Editor nur neu befuellen, wenn die SERVER-DATEN sich geaendert haben.
  // Lokale Editor-Aenderungen (add/remove/key) duerfen nicht ueberschrieben
  // werden — sonst wirkt "Add target" wie ein No-Op.
  const d = termState.data;
  if (!d) return;
  const sig = JSON.stringify((d.targets || []).map(t => [t.name, t.host, t.user, t.has_key, !!t.suggested]));
  if (termState._editorSync === sig) return;
  termState._editorSync = sig;
  termState.editor = (d.targets || []).map(t => ({
    _name: t.name, name: t.name, host: t.host || "", user: t.user || "",
    has_key: !!t.has_key, auto: !!t.suggested, showKey: false,
    keyText: "", deleteKey: false,
  }));
}

function renderTermSettings() {
  const box = document.getElementById("termsettbox");
  if (!box) return;
  const d = termState.data;
  if (!d) {
    box.innerHTML = `<p class="hint">Terminal settings not available.</p>`;
    return;
  }
  syncTermEditor();
  const status = !d.enabled
    ? `<p class="hint" style="color:#fbbf24">🔒 Terminal login not configured — set <code>TTYD_USER</code> and <code>TTYD_PASS</code> as container environment variables (Unraid: Docker → NetSpy → edit → apply), then recreate the container. The Terminal tab stays locked until then; you can still set up targets here.</p>`
    : (d.error ? `<p class="hint" style="color:#fbbf24">⚠️ ${esc(d.error)}</p>` : "");
  const nAuto = (d.targets || []).filter(t => t.suggested).length;
  const autoHint = nAuto
    ? `<p class="hint">🪄 Your monitoring servers are auto-listed as SSH targets below (no user yet). Enter the <b>user</b> and save — the target activates. Auto rows can't be removed here; disable the server in Settings → Server list instead.</p>`
    : "";
  const rows = termState.editor.map((e, i) => `
    <div class="sett-row term-row" data-i="${i}">
      <input class="sett-name" data-f="name" value="${esc(e.name)}" placeholder="Name (e.g. TrueNAS)" ${e.auto ? "disabled title=\"from monitoring servers\"" : ""}>
      <input data-f="host" value="${esc(e.host)}" placeholder="Host/IP" style="min-width:130px">
      <input data-f="user" value="${esc(e.user)}" placeholder="User" style="min-width:90px">
      ${e.auto ? `<span class="hint" style="color:#fbbf24;white-space:nowrap" title="auto-suggested from monitoring servers">🪄 auto</span>` : ""}
      <span class="termkey">
        ${e.has_key ? `<span class="hint">🔑 key set</span>` : `<span class="hint" style="color:#fbbf24">no key</span>`}
        <button class="chip-btn" data-act="key" title="paste an SSH private key (optional)">${e.has_key ? "replace" : "set key"}</button>
        ${e.has_key ? `<button class="chip-btn danger" data-act="delkey">🗑️ key</button>` : ""}
      </span>
      ${e.auto ? "" : `<button class="sett-del" data-act="remove" title="remove target">✕</button>`}
    </div>
    ${e.showKey ? `
    <div class="term-row" data-i="${i}">
      <textarea data-f="key" rows="5" spellcheck="false" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----${String.fromCharCode(10)}… paste the private key (without key → ssh asks the password interactively)">${esc(e.keyText)}</textarea>
    </div>` : ""}
  `).join("") || `<p class="hint">No targets — add one below.</p>`;
  box.innerHTML = `
    ${status}
    ${autoHint}
    <div class="sett-actions" style="margin-bottom:8px">
      <button id="term-add" class="chip-btn">＋ Add target</button>
    </div>
    ${rows}
    <div class="sett-actions">
      <button id="term-save" class="chip-btn">💾 Save targets</button>
      <span id="termsett-msg" class="hint"></span>
    </div>`;
  box.querySelectorAll("input[data-f], textarea[data-f]").forEach(inp => {
    inp.addEventListener("input", () => {
      const e = termState.editor[+inp.closest(".term-row").dataset.i];
      if (inp.dataset.f === "key") e.keyText = inp.value;
      else e[inp.dataset.f] = inp.value;
    });
  });
  box.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.closest(".term-row").dataset.i;
      const e = termState.editor[i];
      if (btn.dataset.act === "remove") termState.editor.splice(i, 1);
      if (btn.dataset.act === "key") e.showKey = !e.showKey;
      if (btn.dataset.act === "delkey") { e.deleteKey = true; e.has_key = false; }
      renderTermSettings();
    });
  });
  const add = document.getElementById("term-add");
  if (add) add.addEventListener("click", () => {
    termState.editor.push({ _name: "", name: "", host: "", user: "",
      has_key: false, auto: false, showKey: false, keyText: "", deleteKey: false });
    renderTermSettings();
  });
  const save = document.getElementById("term-save");
  if (save) save.addEventListener("click", async () => {
    const msg = document.getElementById("termsett-msg");
    const full = e => (e.name || "").trim() && (e.host || "").trim() && (e.user || "").trim();
    const rows = termState.editor;
    const complete = rows.filter(full);
    const skipped = rows.length - complete.length;
    // Auto-Zeilen (Monitoring-Server ohne User) sind nur Vorschlaege: ohne
    // User werden sie nicht gespeichert, sondern erscheinen nach dem Save
    // automatisch wieder. Nur wenn der User ALLE echten/manuellen Zeilen
    // entfernt hat, ist ein leerer POST gewollt (alles loeschen).
    const nonAuto = rows.filter(e => !e.auto);
    if (!complete.length && nonAuto.length) {
      msg.textContent = skipped
        ? `⏸ Nothing to save — ${skipped} row(s) need user + host`
        : "⏸ Nothing to save";
      return;
    }
    const payload = complete.map(e => ({ name: e.name, host: e.host, user: e.user,
      key: e.keyText || undefined, delete_key: e.deleteKey || undefined }));
    try {
      const r = await fetch("/api/terminal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ targets: payload }),
      });
      const res = await r.json();
      if (!r.ok) { msg.textContent = "❌ " + (res.error || r.status); return; }
      const okMsg = skipped
        ? `✅ Saved ${complete.length} target(s) — ${skipped} incomplete row(s) kept as draft`
        : `✅ Saved — ${complete.length} target(s) restarted`;
      termState.data = res;
      termState.lastKey = "";   // Terminal-Tab beim naechsten Oeffnen neu bauen
      syncTermEditor();
      renderTermSettings();
      // renderTermSettings baut die Box neu -> Meldung danach erneut setzen
      const msg2 = document.getElementById("termsett-msg");
      if (msg2) msg2.textContent = okMsg;
    } catch (e) {
      msg.textContent = "❌ save failed";
    }
  });
}
