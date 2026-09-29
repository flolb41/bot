const REFRESH_MS = 30000;
let quote = "USDT";
let equityChart = null;
let pnlChart = null;

const $ = (id) => document.getElementById(id);

const fmt = (v, digits = 2) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("fr-FR", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtMoney = (v, digits = 2) => (v === null || v === undefined ? "—" : `${fmt(v, digits)} ${quote}`);

const fmtDate = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });

const signClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");

const signed = (v, digits = 2) => (v > 0 ? "+" : "") + fmt(v, digits);

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function setText(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = el.className.replace(/\b(pos|neg)\b/g, "").trim();
  if (cls) el.classList.add(cls);
}

// ---------------------------------------------------------------------------
async function loadOverview() {
  const o = await getJSON("/api/overview");
  quote = o.quote_currency;

  const modeBadge = $("mode-badge");
  modeBadge.textContent = o.mode;
  modeBadge.className = `badge ${o.mode}`;

  const aliveBadge = $("alive-badge");
  aliveBadge.textContent = o.bot_alive ? "en ligne" : "hors ligne";
  aliveBadge.className = `badge ${o.bot_alive ? "on" : "off"}`;

  $("meta-exchange").textContent = o.exchange;
  $("meta-symbols").textContent = o.symbols.join(", ");
  $("meta-timeframe").textContent = o.timeframe;
  $("meta-strategy").textContent = o.strategy;

  setText("kpi-balance", fmtMoney(o.balance));
  setText("kpi-equity", fmtMoney(o.equity));
  if (o.equity !== null && o.starting_balance) {
    const totalWithVault = o.equity + o.vault.amount;
    const change = ((totalWithVault - o.starting_balance) / o.starting_balance) * 100;
    setText("kpi-equity-change", `${signed(change)} % depuis le départ (coffre inclus)`, signClass(change));
  }
  setText("kpi-daily", signed(o.daily_pnl) + " " + quote, signClass(o.daily_pnl));
  setText("kpi-total-pnl", signed(o.stats.total_pnl) + " " + quote, signClass(o.stats.total_pnl));
  setText("kpi-trades", `${o.stats.closed_trades} trades clôturés`);
  setText("kpi-winrate", `${fmt(o.stats.win_rate_pct, 1)} %`);
  setText("kpi-fees", `frais : ${fmt(o.stats.total_fees, 4)} ${quote}`);
  setText("kpi-vault", fmtMoney(o.vault.amount));
  setText("kpi-rewards", `récompenses : +${fmt(o.vault.total_rewards, 4)} ${quote}`);
  setText("kpi-positions", `${o.open_positions} / ${o.max_open_positions}`);
  setText("kpi-wallet", o.wallet_address || "non créé");
  setText("kpi-vault-wallet", o.vault_wallet_address || "non créé");
}

// ---------------------------------------------------------------------------
async function loadEquity() {
  const rows = await getJSON("/api/equity?limit=500");
  const labels = rows.map((r) => fmtDate(r.timestamp));
  const equity = rows.map((r) => r.equity);
  const balance = rows.map((r) => r.balance);

  if (!equityChart) {
    equityChart = new Chart($("equity-chart"), {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Equity", data: equity, borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.15)", fill: true, tension: 0.25, pointRadius: 0, borderWidth: 2 },
          { label: "Solde disponible", data: balance, borderColor: "#8b93a5", borderDash: [4, 4], tension: 0.25, pointRadius: 0, borderWidth: 1.5 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: "#e6e9ef" } } },
        scales: {
          x: { ticks: { color: "#8b93a5", maxTicksLimit: 8 }, grid: { color: "#252c3a" } },
          y: { ticks: { color: "#8b93a5" }, grid: { color: "#252c3a" } },
        },
      },
    });
  } else {
    equityChart.data.labels = labels;
    equityChart.data.datasets[0].data = equity;
    equityChart.data.datasets[1].data = balance;
    equityChart.update();
  }
}

async function loadPnlBySymbol() {
  const rows = await getJSON("/api/pnl_by_symbol");
  const labels = rows.map((r) => r.symbol);
  const data = rows.map((r) => r.pnl);
  const colors = data.map((v) => (v >= 0 ? "#22c55e" : "#ef4444"));

  if (!pnlChart) {
    pnlChart = new Chart($("pnl-chart"), {
      type: "bar",
      data: { labels, datasets: [{ label: `PnL (${quote})`, data, backgroundColor: colors }] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#8b93a5" }, grid: { display: false } },
          y: { ticks: { color: "#8b93a5" }, grid: { color: "#252c3a" } },
        },
      },
    });
  } else {
    pnlChart.data.labels = labels;
    pnlChart.data.datasets[0].data = data;
    pnlChart.data.datasets[0].backgroundColor = colors;
    pnlChart.update();
  }
}

// ---------------------------------------------------------------------------
function fillTable(tableId, rows, renderRow, emptyMsg) {
  const tbody = $(tableId).querySelector("tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    tbody.innerHTML = `<tr class="empty"><td colspan="8">${emptyMsg}</td></tr>`;
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = renderRow(r);
    tbody.appendChild(tr);
  }
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function loadPositions() {
  const rows = await getJSON("/api/positions");
  fillTable(
    "positions-table",
    rows,
    (p) => `
      <td><span class="tag">${esc(p.strategy)}</span></td>
      <td>${esc(p.symbol)}</td>
      <td class="num">${fmt(p.entry_price, 4)}</td>
      <td class="num">${fmt(p.amount, 6)}</td>
      <td class="num neg">${fmt(p.stop_loss, 4)}</td>
      <td class="num pos">${fmt(p.take_profit, 4)}</td>
      <td class="num">${p.trailing_stop ? fmt(p.trailing_stop, 4) : "—"}</td>
      <td>${fmtDate(p.opened_at)}</td>`,
    "Aucune position ouverte"
  );
}

async function loadTrades() {
  const rows = await getJSON("/api/trades?limit=50");
  fillTable(
    "trades-table",
    rows,
    (t) => `
      <td>${fmtDate(t.timestamp)}</td>
      <td><span class="tag">${esc(t.strategy)}</span></td>
      <td>${esc(t.symbol)}</td>
      <td class="side-${esc(t.side)}">${t.side === "buy" ? "ACHAT" : "VENTE"}</td>
      <td class="num">${fmt(t.price, 4)}</td>
      <td class="num">${fmt(t.amount, 6)}</td>
      <td class="num ${t.side === "sell" ? signClass(t.pnl) : ""}">${t.side === "sell" ? signed(t.pnl, 4) : "—"}</td>
      <td>${esc(t.reason)}</td>`,
    "Aucun trade enregistré"
  );
}

async function loadStrategies() {
  const rows = await getJSON("/api/pnl_by_strategy");
  fillTable(
    "strategies-table",
    rows,
    (s) => `
      <td><span class="tag">${esc(s.strategy)}</span></td>
      <td class="num">${s.n}</td>
      <td class="num">${s.wins}</td>
      <td class="num">${s.n ? fmt((s.wins / s.n) * 100, 0) : "—"} %</td>
      <td class="num ${signClass(s.pnl)}">${signed(s.pnl, 4)} ${quote}</td>`,
    "Aucun trade clôturé"
  );
}

async function loadSpreads() {
  const rows = await getJSON("/api/spreads");
  fillTable(
    "spreads-table",
    rows,
    (s) => `
      <td>${esc(s.symbol)}</td>
      <td class="num">${fmt(s.cex_price, 4)}</td>
      <td class="num">${fmt(s.dex_price, 4)}</td>
      <td class="num ${Math.abs(s.spread_pct) >= 1 ? "neg" : ""}">${signed(s.spread_pct, 3)} %</td>
      <td>${fmtDate(s.updated_at)}</td>`,
    "Cotation DEX désactivée ou pas encore disponible"
  );
}

async function loadLogs() {
  if ($("logs").classList.contains("hidden")) return;
  const lines = await getJSON("/api/logs?lines=150");
  const pre = $("logs");
  pre.textContent = lines.join("");
  pre.scrollTop = pre.scrollHeight;
}

// ---------------------------------------------------------------------------
async function refreshAll() {
  const tasks = [loadOverview, loadEquity, loadPnlBySymbol, loadStrategies, loadSpreads, loadPositions, loadTrades, loadLogs];
  for (const task of tasks) {
    try {
      await task();
    } catch (err) {
      console.error(err);
    }
  }
  $("last-refresh").textContent = new Date().toLocaleTimeString("fr-FR");
}

$("toggle-logs").addEventListener("click", () => {
  const pre = $("logs");
  pre.classList.toggle("hidden");
  $("toggle-logs").textContent = pre.classList.contains("hidden") ? "Afficher" : "Masquer";
  loadLogs().catch(console.error);
});

refreshAll();
setInterval(refreshAll, REFRESH_MS);
