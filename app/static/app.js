/* NetMon Frontend — charts + sortable process list, 1s refresh */
"use strict";

const state = {
  sortKey: "rx", sortDir: -1, servers: [], charts: {},
  ifaceSort: {}, lastIfaces: null, lastTable: [], visible: {},
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
      `<span class="lg tx">▲ out ${fmt(0)}</span></div>`;
    grid.appendChild(card);
  }

  for (const s of servers) {
    const canvas = grid.querySelector("#chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_") + " canvas");
    state.charts[s.name] = new Chart(canvas, {
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
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#64748b", maxTicksLimit: 6, maxRotation: 0 }, grid: { color: "rgba(255,255,255,.05)" } },
          y: { ticks: { color: "#64748b", callback: v => fmt(v) }, grid: { color: "rgba(255,255,255,.05)" }, beginAtZero: true },
        },
        interaction: { intersect: false, mode: "index" },
      },
    });
  }
}

function updateCharts(series) {
  for (const name of state.servers) {
    const ch = state.charts[name];
    if (!ch) continue;
    const s = series[name] || { ts: [], rx: [], tx: [] };
    const n = Math.min(300, s.ts.length);
    ch.data.labels = s.ts.slice(-n).map(fmtTs);
    ch.data.datasets[0].data = s.rx.slice(-n);
    ch.data.datasets[1].data = s.tx.slice(-n);
    ch.update("none");
  }
}

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
    badge.textContent = s.online ? "online · ▼ " + fmt(t.rx) + " ▲ " + fmt(t.tx) : "offline";
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
  if (state.lastTable) renderTable(state.lastTable, state.servers.map(n => ({ name: n })));
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
    return `<tr${isRest ? ' class="restrow"' : ""}>` +
      `<td class="pname${isRest ? " rest" : ""}">${esc(r.name)}${badge}</td>` +
      `<td class="srv">${esc(sname)}</td>` +
      `<td class="num rx">${h.rx == null ? "–" : fmt(h.rx)}</td>` +
      `<td class="num tx">${h.tx == null ? "–" : fmt(h.tx)}</td></tr>`;
  }).join("");
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
    buildServerFilter(d.servers);
  }
  state.version = d.version || state.version;
  state.lastIfaces = d.ifaces;
  state.lastTable = d.table;
  renderStatusbar(d.servers);
  updateCharts(d.series);
  renderTable(d.table, d.servers);
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

refresh();
setInterval(refresh, 1000);
