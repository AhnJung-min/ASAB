// ASAB 단타 대시보드 프론트엔드
const $ = (s) => document.querySelector(s);
const fmt = (n, d = 0) => (n == null ? "—" : Number(n).toLocaleString("ko-KR",
  { minimumFractionDigits: d, maximumFractionDigits: d }));
const sign = (n, d = 1) => (n == null ? "—" : (n >= 0 ? "+" : "") + Number(n).toFixed(d));
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "neu");
async function api(p) { const r = await fetch(p); return r.json(); }

let charts = {};
function chart(id, cfg) { if (charts[id]) charts[id].destroy(); charts[id] = new Chart($("#" + id), cfg); }
const AX = { ticks: { color: "#8b95a7", font: { size: 10 } }, grid: { color: "#242c3d" } };
const noLegend = { plugins: { legend: { display: false } } };

// ---------- 탭 ----------
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".page").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $("#" + t.dataset.tab).classList.add("active");
  if (t.dataset.tab === "symbol") loadSymbols();
  if (t.dataset.tab === "rebound") loadRebound();
  if (t.dataset.tab === "scan") loadScan();
  if (t.dataset.tab === "trades") loadTrades();
  if (t.dataset.tab === "account") loadAccount();
});

// ---------- 실제 계좌 (라이브) ----------
async function loadAccount() {
  $("#acctTs").textContent = "조회 중…";
  const d = await api("/api/live_account");
  if (d.error) { $("#acctKpis").innerHTML = `<div class="card"><div class="label">조회 실패</div><div class="value neg" style="font-size:15px">${d.error}</div></div>`; $("#acctTable").innerHTML = ""; $("#acctTs").textContent = ""; return; }
  $("#acctTs").textContent = d.ts;
  const cards = [
    ["총자산", fmt(d.total) + "원", ""],
    ["예수금(현금)", fmt(d.cash) + "원", "neu"],
    ["평가금액", fmt(d.holdings_krw) + "원", ""],
    ["평가손익", sign(d.unrealized, 0) + "원", cls(d.unrealized)],
    ["보유 종목", d.holdings.length + "종목", "neu"],
  ];
  $("#acctKpis").innerHTML = cards.map(([l, v, c]) =>
    `<div class="card"><div class="label">${l}</div><div class="value ${c}">${v}</div></div>`).join("");
  $("#acctTable").innerHTML = thead(["종목", "코드", "수량", "평균단가", "평가금액", "손익률"]) +
    "<tbody>" + d.holdings.map((h) => `<tr><td>${h.name}</td><td>${h.symbol}</td>
      <td>${fmt(h.qty)}</td><td>${fmt(h.avg_price)}</td><td>${fmt(h.eval_amt)}</td>
      <td class="${cls(h.pnl_pct)}">${h.pnl_pct == null ? "—" : sign(h.pnl_pct)}%</td></tr>`).join("") + "</tbody>";
}

// ---------- 개요 ----------
async function loadOverview() {
  const d = await api("/api/overview");
  $("#today").textContent = d.today;
  const ret = d.total_krw ? (d.realized_krw + d.unrealized_krw) : 0;
  const ic = d.model && d.model.oos_ic != null ? d.model.oos_ic : null;
  const cards = [
    ["현재 총자산", fmt(d.total_krw) + "원", "", ""],
    ["실현손익(오늘)", sign(d.realized_krw, 0) + "원", cls(d.realized_krw), ""],
    ["평가손익", sign(d.unrealized_krw, 0) + "원", cls(d.unrealized_krw), ""],
    ["보유 / 청산", d.open_positions + " / " + d.closed_today, "neu", "오늘 매매"],
    ["승률(익절)", d.winrate + "%", d.winrate >= 50 ? "pos" : "neg", ""],
    ["스캔 수집", fmt(d.scans_today), "neu", "반등후보 " + d.rebound_symbols + "종목"],
    ["분봉", fmt(d.minute_bars), "neu", "오늘 수집"],
    ["모델 OOS IC", ic == null ? "—" : sign(ic, 3), ic == null ? "" : (ic > 0.05 ? "pos" : "neu"),
      d.model ? (d.model.kind || "") : "모델없음"],
  ];
  $("#kpis").innerHTML = cards.map(([l, v, c, s]) =>
    `<div class="card"><div class="label">${l}</div><div class="value ${c}">${v}</div>
     <div class="sub">${s}</div></div>`).join("");

  // 자산곡선
  const cv = await api("/api/account_curve");
  chart("curveChart", {
    type: "line",
    data: { labels: cv.map((r) => r.ts.slice(11, 16)),
      datasets: [{ data: cv.map((r) => r.total_krw), borderColor: "#4c9aff",
        backgroundColor: "rgba(76,154,255,.08)", fill: true, tension: .3,
        pointRadius: 0, borderWidth: 2 }] },
    options: { ...noLegend, scales: { x: AX, y: AX } },
  });

  // 청산 사유
  const colors = { "익절": "#26c281", "손절": "#e8506e", "트레일링": "#f0b429", "시간초과": "#4c9aff" };
  chart("reasonChart", {
    type: "doughnut",
    data: { labels: d.reasons.map((r) => r.reason),
      datasets: [{ data: d.reasons.map((r) => r.n),
        backgroundColor: d.reasons.map((r) => colors[r.reason] || "#888"), borderWidth: 0 }] },
    options: { plugins: { legend: { position: "right", labels: { color: "#eef1f7", boxWidth: 12 } } } },
  });

  // 활동
  const ac = await api("/api/activity");
  const hours = [...new Set([...ac.buys.map((b) => b.hh), ...ac.scans.map((s) => s.hh)])].sort();
  const mapN = (arr) => hours.map((h) => (arr.find((x) => x.hh === h) || {}).n || 0);
  chart("activityChart", {
    type: "bar",
    data: { labels: hours.map((h) => h + "시"),
      datasets: [
        { label: "매수", data: mapN(ac.buys), backgroundColor: "#26c281" },
        { label: "스캔", data: mapN(ac.scans), backgroundColor: "#2c3850", yAxisID: "y2" }] },
    options: { scales: { x: AX, y: { ...AX, position: "left" },
      y2: { ...AX, position: "right", grid: { display: false } } },
      plugins: { legend: { labels: { color: "#8b95a7" } } } },
  });
}

// ---------- 매매 ----------
async function loadTrades() {
  const pos = await api("/api/positions");
  $("#posCount").textContent = pos.length + "종목";
  $("#posTable").innerHTML = thead(["종목", "수량", "진입가", "진입율", "호가임밸", "진입시각"]) +
    "<tbody>" + pos.map((p) => `<tr><td>${p.name}</td><td>${p.qty}</td>
      <td>${fmt(p.entry_price)}</td><td>${sign(p.entry_rate)}%</td>
      <td>${p.entry_ob_imbalance == null ? "—" : sign(p.entry_ob_imbalance, 2)}</td>
      <td>${(p.entry_ts || "").slice(11, 19)}</td></tr>`).join("") + "</tbody>";

  const tr = await api("/api/trades");
  $("#tradeTable").innerHTML = thead(["종목", "진입", "청산", "손익률", "사유", "보유", "청산시각"]) +
    "<tbody>" + tr.map((t) => `<tr><td>${t.name}</td><td>${fmt(t.entry_price)}</td>
      <td>${fmt(t.exit_price)}</td><td class="${cls(t.pnl_pct)}">${sign(t.pnl_pct)}%</td>
      <td><span class="tag ${t.reason}">${t.reason}</span></td>
      <td>${t.hold_sec ? Math.round(t.hold_sec / 60) + "분" : "—"}</td>
      <td>${(t.exit_ts || "").slice(11, 19)}</td></tr>`).join("") + "</tbody>";
}

// ---------- 스캔 ----------
async function loadScan() {
  const d = await api("/api/scan_latest");
  $("#scanTs").textContent = d.ts ? d.ts.slice(11, 19) : "";
  $("#scanTable").innerHTML = thead(["종목", "코드", "등락률", "거래량", "소스"]) +
    "<tbody>" + d.rows.map((r) => `<tr><td>${r.name}</td><td>${r.symbol}</td>
      <td class="${cls(r.rate)}">${sign(r.rate)}%</td><td>${fmt(r.volume)}</td>
      <td><span class="tag ${r.source}">${r.source}</span></td></tr>`).join("") + "</tbody>";
}

// ---------- 종목 차트 ----------
let symLoaded = false;
async function loadSymbols() {
  if (symLoaded) return; symLoaded = true;
  const syms = await api("/api/symbols");
  $("#symSelect").innerHTML = syms.map((s) =>
    `<option value="${s.symbol}">${s.name} (${s.symbol})</option>`).join("");
  $("#symSelect").onchange = () => drawSymbol($("#symSelect").value);
  if (syms.length) drawSymbol(syms[0].symbol);
}
async function drawSymbol(sym) {
  const d = await api("/api/minute/" + sym);
  const t = d.trades.length ? d.trades[d.trades.length - 1] : null;
  $("#symMeta").innerHTML = `${d.name} · 분봉 ${d.bars.length}개` +
    (t ? ` · 최근매매 ${sign(t.pnl_pct)}% [${t.reason || "보유중"}]` : " · 매매없음(스캔만)");
  chart("symChart", {
    type: "line",
    data: { labels: d.bars.map((b) => b.time.slice(0, 2) + ":" + b.time.slice(2, 4)),
      datasets: [{ data: d.bars.map((b) => b.close), borderColor: "#4c9aff",
        backgroundColor: "rgba(76,154,255,.06)", fill: true, pointRadius: 0,
        borderWidth: 1.5, tension: .2 }] },
    options: { ...noLegend, scales: { x: { ...AX, ticks: { ...AX.ticks, maxTicksLimit: 12 } }, y: AX } },
  });
}

// ---------- 하락·반등 ----------
async function loadRebound() {
  const d = await api("/api/rebound");
  $("#rebTable").innerHTML = thead(["종목", "코드", "장중최저등락", "저점후 반등폭"]) +
    "<tbody>" + d.map((r) => `<tr><td>${r.name}</td><td>${r.symbol}</td>
      <td class="neg">${sign(r.minrate)}%</td>
      <td class="${r.bounce_pct >= 3 ? "pos" : ""}">${r.bounce_pct == null ? "—" : "+" + r.bounce_pct + "%"}</td>
      </tr>`).join("") + "</tbody>";
}

function thead(cols) { return "<thead><tr>" + cols.map((c) => `<th>${c}</th>`).join("") + "</tr></thead>"; }

// ---------- 새로고침 ----------
function refreshActive() {
  const t = document.querySelector(".tab.active").dataset.tab;
  loadOverview();
  if (t === "trades") loadTrades();
  if (t === "scan") loadScan();
  if (t === "rebound") loadRebound();
}
$("#refresh").onclick = refreshActive;
let timer = setInterval(() => { if ($("#autoref").checked) refreshActive(); }, 15000);

loadOverview();
loadTrades();
