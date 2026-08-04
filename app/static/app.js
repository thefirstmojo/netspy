/* NetMon Frontend — Charts + sortierbare Prozessliste, 1s Refresh */
"use strict";

const state = { sortKey: "total", sortDir: -1, servers: [], charts: {}, ifaceSort: {}, lastIfaces: null };

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
      `<span class="lg rx">▼ rein ${fmt(0)}</span>` +
      `<span class="lg tx">▲ raus ${fmt(0)}</span></div>`;
    grid.appendChild(card);
  }

  for (const s of servers) {
    const canvas = grid.querySelector("#chart-" + s.name.replace(/[^a-zA-Z0-9]/g, "_") + " canvas");
    state.charts[s.name] = new Chart(canvas, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { label: "rein", data: [], borderColor: COLORS.rx, backgroundColor: "rgba(34,211,238,.12)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
          { label: "raus", data: [], borderColor: COLORS.tx, backgroundColor: "rgba(245,158,11,.10)", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 },
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
    if (lg.length === 2) { lg[0].textContent = "▼ rein " + fmt(t.rx); lg[1].textContent = "▲ raus " + fmt(t.tx); }
  }
}

/* ---------- Prozess-Tabelle ---------- */
function buildTableHeader(servers) {
  const thead = document.getElementById("procthead");
  let html = `<tr><th data-key="name" class="sortable ${state.sortKey === "name" ? "active" : ""}">Prozess</th>`;
  for (const s of servers) {
    const kIn = "in:" + s.name, kOut = "out:" + s.name;
    const clsIn = state.sortKey === kIn ? "active" + (state.sortDir < 0 ? " sort-desc" : "") : "";
    const clsOut = state.sortKey === kOut ? "active" + (state.sortDir < 0 ? " sort-desc" : "") : "";
    html += `<th data-key="${kIn}" class="sortable num ${clsIn}">${esc(s.name)}<br><span class="sub">rein ▼</span></th>`;
    html += `<th data-key="${kOut}" class="sortable num ${clsOut}">${esc(s.name)}<br><span class="sub">raus ▲</span></th>`;
  }
  html += `<th data-key="total" class="sortable num ${state.sortKey === "total" ? "active" : ""}">Total</th>`;
  thead.innerHTML = html;
}

function renderTable(table, servers) {
  const tbody = document.getElementById("proctbody");
  const rows = [...table].sort((a, b) => {
    let av, bv;
    if (state.sortKey === "name") { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
    else if (state.sortKey === "total") { av = a.total; bv = b.total; }
    else {
      const [dir, sname] = state.sortKey.split(":");
      av = (a.hosts[sname] || {})[dir === "in" ? "rx" : "tx"] || 0;
      bv = (b.hosts[sname] || {})[dir === "in" ? "rx" : "tx"] || 0;
    }
    if (av < bv) return -1 * state.sortDir;
    if (av > bv) return 1 * state.sortDir;
    return 0;
  });

  tbody.innerHTML = rows.map(r => {
    const isRest = r.kind === "rest";
    let badge = "";
    if (r.kind === "container") badge = `<span class="cont">Container</span>`;
    else if (r.container) badge = `<span class="cont">${esc(r.container)}</span>`;
    let cells = `<td class="pname${isRest ? " rest" : ""}">${esc(r.name)}${badge}</td>`;
    let total = 0;
    for (const s of servers) {
      const h = r.hosts[s.name];
      const rx = h ? h.rx : null, tx = h ? h.tx : null;
      cells += `<td class="num rx">${rx == null ? "–" : fmt(rx)}</td>`;
      cells += `<td class="num tx">${tx == null ? "–" : fmt(tx)}</td>`;
      total += (rx || 0) + (tx || 0);
    }
    cells += `<td class="num total">${fmt(total)}</td>`;
    return `<tr${isRest ? ' class="restrow"' : ""}>${cells}</tr>`;
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
      if (st.key === "name") { av = a.name.toLowerCase(); bv = b.name.toLowerCase(); }
      else { av = a[st.key] || 0; bv = b[st.key] || 0; }
      if (av < bv) return -st.dir;
      if (av > bv) return st.dir;
      return 0;
    });
    const cls = k => "sortable" + (k !== "name" ? " num" : "") +
      (st.key === k ? " active" + (st.dir < 0 ? " sort-desc" : "") : "");
    const rows = list.map(i =>
      `<tr><td class="pname">${esc(i.name)}${i.uplink ? ' <span class="uplink">UPLINK</span>' : ""}` +
      (i.container ? ` <span class="cont">${esc(i.container)}</span>` : "") + `</td>` +
      `<td class="num rx">${fmt(i.rx)}</td><td class="num tx">${fmt(i.tx)}</td></tr>`).join("");
    return `<details class="card" data-server="${esc(s.name)}"${wasOpen[s.name] ? " open" : ""}>` +
      `<summary>Interfaces · ${esc(s.name)} (${list.length}) <span class="hint">— Kopf klickbar zum Sortieren</span></summary>` +
      `<table class="ifacetable"><thead><tr>` +
      `<th class="${cls("name")}" data-key="name">Interface</th>` +
      `<th class="${cls("rx")}" data-key="rx">rein</th>` +
      `<th class="${cls("tx")}" data-key="tx">raus</th>` +
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
  }
  state.version = d.version || state.version;
  state.lastIfaces = d.ifaces;
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
  else { state.sortKey = key; state.sortDir = key === "name" ? 1 : -1; }
  buildTableHeader(state.servers.map(n => ({ name: n })));
});

refresh();
setInterval(refresh, 1000);
