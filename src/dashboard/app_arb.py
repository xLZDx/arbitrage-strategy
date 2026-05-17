"""
Standalone Flask app for the arbitrage dashboard.

Default port: config.DASHBOARD_PORT_FALLBACK (5002).

Per Q5: the canonical plan is to mount arb_blueprint into the sister
trading-bot Flask app at port 5000. This standalone runner is the Q5
fallback path and the Phase 1 default — it lets us validate the API
without touching the live bot's process. The trading-bot dashboard's
"Arbitrage" tab reverse-proxies to this app via http://127.0.0.1:5000/arb/.

Run:
  python -m src.dashboard.app_arb [--port 5002] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import logging
import os

from flask import Flask, jsonify, render_template_string

from src.dashboard.arb_blueprint import bp as arb_bp
from src.utils import config

log = logging.getLogger(__name__)


INDEX_HTML = r"""<!doctype html>
<html><head>
<meta charset="utf-8">
<title>arbitrage — control panel</title>
<style>
  /* Style system mirrored from D:\test 2\AI trading assistance dashboard. */
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;color:#e2e8f0;
       font-family:Inter,-apple-system,system-ui,sans-serif;font-size:13px;
       background:radial-gradient(1200px 800px at 80% -10%,rgba(37,99,235,.08),transparent 60%),
                  linear-gradient(180deg,#050816 0%,#0a0e1f 100%)}
  .topbar{padding:14px 24px;border-bottom:1px solid rgba(148,163,184,.1);
          display:flex;align-items:center;gap:20px;background:rgba(5,8,22,.6);flex-wrap:wrap}
  .topbar h1{margin:0;font-size:.95rem;font-weight:800;color:#fff}
  .topbar .sub{color:#64748b;font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;font-weight:700}
  .pill{padding:3px 10px;border-radius:999px;font-size:.62rem;font-weight:700;
        border:1px solid rgba(148,163,184,.2);background:rgba(15,23,42,.6);font-family:'JetBrains Mono',monospace}
  .pill.green{border-color:rgba(52,211,153,.3);background:rgba(52,211,153,.1);color:#34d399}
  .pill.red{border-color:rgba(251,113,133,.3);background:rgba(251,113,133,.1);color:#fb7185}
  .pill.amber{border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.1);color:#fbbf24}
  .pill.blue{border-color:rgba(96,165,250,.3);background:rgba(96,165,250,.1);color:#60a5fa}
  .grid{display:grid;gap:16px;padding:20px;
        grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
  .card{background:linear-gradient(180deg,rgba(15,23,42,.85),rgba(13,20,40,.72));
        border:1px solid rgba(148,163,184,.13);border-radius:13px;overflow:hidden;
        display:flex;flex-direction:column}
  .card-header{padding:11px 16px;border-bottom:1px solid rgba(148,163,184,.08);
               display:flex;align-items:center;gap:10px}
  .card-title{font-size:.85rem;font-weight:800;color:#fff;letter-spacing:-.01em;flex:1}
  .card-sub{font-size:.58rem;color:#475569;text-transform:uppercase;letter-spacing:.12em;font-weight:700}
  .card-body{padding:14px 16px;display:flex;flex-direction:column;gap:10px}
  table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono','Cascadia Code',monospace;font-size:.72rem}
  th{font-size:.55rem;color:#475569;text-transform:uppercase;letter-spacing:.1em;
     text-align:right;padding:5px 8px;font-weight:700;border-bottom:1px solid rgba(148,163,184,.1)}
  th.left,td.left{text-align:left}
  td{padding:5px 8px;border-bottom:1px solid rgba(148,163,184,.05);text-align:right;
     font-variant-numeric:tabular-nums}
  td.pair{color:#d2a8ff;font-weight:700;text-align:left}
  .pos{color:#34d399}
  .neg{color:#fb7185}
  .meta{color:#64748b;font-size:.62rem}
  pre{background:rgba(5,8,22,.7);padding:10px;border:1px solid rgba(148,163,184,.08);
      border-radius:8px;font-size:.66rem;color:#94a3b8;overflow-x:auto;
      font-family:'JetBrains Mono',monospace;max-height:200px;overflow-y:auto}
  button.btn{background:linear-gradient(135deg,rgba(37,99,235,.18),rgba(139,92,246,.12));
             color:#e2e8f0;border:1px solid rgba(59,130,246,.3);
             padding:7px 14px;border-radius:8px;cursor:pointer;font-size:.7rem;
             font-weight:700;transition:all .15s;font-family:inherit}
  button.btn:hover{background:linear-gradient(135deg,rgba(37,99,235,.28),rgba(139,92,246,.2));
                   border-color:rgba(59,130,246,.5)}
  button.btn:disabled{opacity:.4;cursor:not-allowed}
  button.btn.danger{background:rgba(251,113,133,.12);border-color:rgba(251,113,133,.3);color:#fb7185}
  button.btn.danger:hover{background:rgba(251,113,133,.2)}
  button.btn.green{background:rgba(52,211,153,.12);border-color:rgba(52,211,153,.3);color:#34d399}
  button.btn.green:hover{background:rgba(52,211,153,.2)}
  .btn-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .stat{display:flex;flex-direction:column;gap:2px}
  .stat-label{font-size:.55rem;color:#64748b;text-transform:uppercase;letter-spacing:.1em;font-weight:700}
  .stat-val{font-size:1.1rem;font-weight:800;color:#e2e8f0;font-variant-numeric:tabular-nums}
  .stat-val.big{font-size:1.4rem}
  .row-stats{display:flex;gap:24px;flex-wrap:wrap;padding-bottom:4px}
  input[type=number],input[type=text]{
    background:rgba(5,8,22,.6);border:1px solid rgba(148,163,184,.15);
    color:#e2e8f0;padding:6px 10px;border-radius:7px;font-size:.7rem;
    font-family:'JetBrains Mono',monospace;width:120px}
  label.toggle{display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-size:.7rem;color:#94a3b8}
  label.toggle input{accent-color:#3b82f6}
  .spinner{display:inline-block;width:10px;height:10px;border:2px solid #3b82f6;
           border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite;
           vertical-align:middle;margin-left:4px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .check-list{font-size:.66rem;color:#94a3b8;display:flex;flex-direction:column;gap:2px}
  .check-list .ok{color:#34d399}
  .check-list .bad{color:#fb7185}
</style>
</head><body>
<div class="topbar">
  <h1>⚖ arbitrage</h1>
  <span class="sub">mode</span><span class="pill blue" id="p-mode">?</span>
  <span class="sub">ingestion</span><span class="pill" id="p-ingest">?</span>
  <span class="sub">halt</span><span class="pill" id="p-halt">?</span>
  <span class="sub">gas gwei</span><span class="pill" id="p-gas">?</span>
  <span style="margin-left:auto" class="meta">refresh 3s · proxied at /arb/ on :5000</span>
</div>

<div class="grid">

  <!-- Live spreads -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">Live spreads</div>
      <span class="card-sub">bybit vs dex</span>
    </div>
    <div class="card-body">
      <table id="spread-table">
        <thead><tr>
          <th class="left">pair</th><th>bybit mid</th><th>dex mid</th>
          <th>spread bps</th><th class="left">ts</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Risk & control -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">Risk &amp; control</div>
      <span class="card-sub">phase 4</span>
    </div>
    <div class="card-body">
      <div class="row-stats">
        <div class="stat"><span class="stat-label">today pnl</span><span class="stat-val" id="risk-pnl">$0</span></div>
        <div class="stat"><span class="stat-label">daily cap</span><span class="stat-val" id="risk-cap">$0</span></div>
        <div class="stat"><span class="stat-label">per trade</span><span class="stat-val" id="risk-trade">$0</span></div>
      </div>
      <div class="btn-row">
        <button class="btn danger" onclick="setHalt(true)">SET HALT</button>
        <button class="btn green" onclick="setHalt(false)">CLEAR HALT</button>
        <button class="btn" onclick="runDrill()">Run drill</button>
        <span id="drill-result" class="meta"></span>
      </div>
      <div id="drill-checks" class="check-list"></div>
      <label class="toggle">
        <input type="checkbox" id="maker-toggle" onchange="toggleMaker(this.checked)">
        prefer maker orders (1 bps vs 10 bps Bybit fee)
      </label>
      <div class="meta">maker toggle takes effect on next ingestion restart</div>
    </div>
  </div>

  <!-- Opportunity feed -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">Opportunity feed</div>
      <span class="card-sub">phase 2</span>
    </div>
    <div class="card-body">
      <div class="row-stats">
        <div class="stat"><span class="stat-label">GO</span><span class="stat-val" id="go-count">0</span></div>
        <div class="stat"><span class="stat-label">SKIP</span><span class="stat-val" id="skip-count">0</span></div>
        <div class="stat"><span class="stat-label">theo PnL</span><span class="stat-val big pos" id="pnl-total">$0</span></div>
      </div>
      <table id="pnl-by-pair">
        <thead><tr>
          <th class="left">pair</th><th>GO</th><th>SKIP</th>
          <th>cum USD</th><th>avg bps</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div style="max-height:200px;overflow-y:auto">
      <table id="opp-table">
        <thead><tr>
          <th class="left">ts</th><th class="left">pair</th><th class="left">decision</th>
          <th class="left">reason</th><th>net bps</th><th>$ pnl</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- Replay simulator -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">Replay simulator</div>
      <span class="card-sub">phase 3</span>
    </div>
    <div class="card-body">
      <div class="row-stats">
        <div class="stat"><span class="stat-label">filled</span><span class="stat-val" id="sim-filled">0</span></div>
        <div class="stat"><span class="stat-label">hit rate</span><span class="stat-val" id="sim-hit">0%</span></div>
        <div class="stat"><span class="stat-label">cum PnL</span><span class="stat-val pos" id="sim-cum">$0</span></div>
        <div class="stat"><span class="stat-label">sharpe</span><span class="stat-val" id="sim-sharpe">—</span></div>
      </div>
      <div class="btn-row">
        <button class="btn" id="btn-replay" onclick="runReplay()">Run replay (writes sim_trades)</button>
        <span id="replay-msg" class="meta"></span>
      </div>
      <table id="sim-by-pair">
        <thead><tr>
          <th class="left">pair</th><th>filled</th>
          <th>cum USD</th><th>avg bps</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Counter-factual -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">Counter-factual analysis</div>
      <span class="card-sub">what if</span>
    </div>
    <div class="card-body">
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:end">
        <div>
          <div class="stat-label">bybit fee bps</div>
          <input type="number" id="cf-fee" value="1" min="0" max="50" step="0.5">
        </div>
        <div>
          <div class="stat-label">notional usd</div>
          <input type="number" id="cf-notional" value="50" min="10" max="10000" step="50">
        </div>
        <button class="btn" id="btn-cf" onclick="runCf()">Recompute</button>
      </div>
      <div class="row-stats">
        <div class="stat"><span class="stat-label">would have GO</span><span class="stat-val" id="cf-go">—</span></div>
        <div class="stat"><span class="stat-label">would have SKIP</span><span class="stat-val" id="cf-skip">—</span></div>
        <div class="stat"><span class="stat-label">theo PnL</span><span class="stat-val pos" id="cf-pnl">—</span></div>
      </div>
      <table id="cf-by-pair">
        <thead><tr>
          <th class="left">pair</th><th>GO</th><th>SKIP</th>
          <th>cum USD</th><th>best net bps</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="meta">Recompute decisions against the captured opportunities table with new fee/notional.</div>
    </div>
  </div>

  <!-- HistGBT -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">HistGBT classifier</div>
      <span class="card-sub">phase 6</span>
    </div>
    <div class="card-body">
      <div class="row-stats">
        <div class="stat"><span class="stat-label">loaded</span><span class="stat-val" id="m-loaded">—</span></div>
        <div class="stat"><span class="stat-label">AUC</span><span class="stat-val" id="m-auc">—</span></div>
        <div class="stat"><span class="stat-label">n_train</span><span class="stat-val" id="m-ntrain">—</span></div>
        <div class="stat"><span class="stat-label">veto thr</span><span class="stat-val" id="m-thr">—</span></div>
      </div>
      <div class="btn-row">
        <button class="btn" id="btn-train" onclick="runTrain()">Train</button>
        <span id="train-msg" class="meta"></span>
      </div>
      <div class="meta">Requires sim_trades populated (run replay first).</div>
    </div>
  </div>

  <!-- Soak summary -->
  <div class="card" style="grid-column:1 / -1">
    <div class="card-header">
      <div class="card-title">Soak summary</div>
      <span class="card-sub">tables · spreads · drift</span>
    </div>
    <div class="card-body">
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <div style="flex:1;min-width:280px">
          <div class="card-sub">data tables</div>
          <table id="soak-tables"><tbody></tbody></table>
        </div>
        <div style="flex:1;min-width:280px">
          <div class="card-sub">spread distribution (bps)</div>
          <table id="soak-spreads">
            <thead><tr><th class="left">pair</th><th>n</th><th>min</th><th>med</th><th>max</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div class="card-sub" style="margin-top:8px">last drift alerts</div>
      <pre id="drift-log">—</pre>
    </div>
  </div>

</div>

<script>
async function jget(url){return (await fetch(url)).json()}
async function jpost(url,body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                              body: body ? JSON.stringify(body) : '{}'});
  return r.json();
}
function pill(id, cls, txt){
  const el = document.getElementById(id);
  el.textContent = txt;
  el.className = 'pill ' + cls;
}
function clsAmt(n){return (n||0) >= 0 ? 'pos' : 'neg'}

async function refresh() {
  try {
    const [h, s, g, opp, pnl, sim, risk, soak, model] = await Promise.all([
      jget('/api/arb/health'),
      jget('/api/arb/spread'),
      jget('/api/arb/gas'),
      jget('/api/arb/opportunities?n=30'),
      jget('/api/arb/pnl_simulated'),
      jget('/api/arb/sim_summary'),
      jget('/api/arb/risk'),
      jget('/api/arb/soak_summary'),
      jget('/api/arb/model_status'),
    ]);
    // Top bar
    pill('p-mode', h.mode === 'SHADOW' ? 'blue' : (h.mode === 'MAINNET' ? 'red' : 'amber'), h.mode);
    pill('p-ingest', h.ingestion_running ? 'green' : 'red',
         h.ingestion_running ? ('pid ' + h.ingestion_pid) : 'DOWN');
    pill('p-halt', h.halt_active ? 'red' : 'green', h.halt_active ? 'HALT' : 'ok');
    pill('p-gas', 'blue', g.gas ? g.gas.total_gas_price_gwei.toFixed(4) : '—');

    // Live spreads
    const tbody = document.querySelector('#spread-table tbody');
    tbody.innerHTML = '';
    for (const row of (s.spreads || [])) {
      const tr = document.createElement('tr');
      const cls = row.spread_bps == null ? 'meta' : (row.spread_bps > 0 ? 'pos' : 'neg');
      tr.innerHTML = `
        <td class="pair">${row.pair}</td>
        <td>${row.bybit_mid?.toLocaleString() ?? '—'}</td>
        <td>${row.dex_mid?.toLocaleString() ?? '—'}</td>
        <td class="${cls}">${row.spread_bps != null ? row.spread_bps.toFixed(2) : '—'}</td>
        <td class="meta">${(row.bybit_ts ?? '').slice(11,19)}</td>`;
      tbody.appendChild(tr);
    }

    // Risk card
    document.getElementById('risk-pnl').textContent =
      '$' + (risk.today_realized_pnl_usd ?? 0).toFixed(2);
    document.getElementById('risk-cap').textContent = '$' + (risk.daily_loss_cap_usd ?? 0);
    document.getElementById('risk-trade').textContent = '$' + (risk.per_trade_cap_usd ?? 0);

    // Opportunity card
    document.getElementById('go-count').textContent = (pnl.go_count ?? 0).toLocaleString();
    document.getElementById('skip-count').textContent = (pnl.skip_count ?? 0).toLocaleString();
    const pTot = pnl.cumulative ?? 0;
    const pEl = document.getElementById('pnl-total');
    pEl.textContent = '$' + pTot.toFixed(4);
    pEl.className = 'stat-val big ' + clsAmt(pTot);
    const ptbody = document.querySelector('#pnl-by-pair tbody');
    ptbody.innerHTML = '';
    for (const row of (pnl.by_pair || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="pair">${row.pair}</td>
        <td>${row.go_count.toLocaleString()}</td>
        <td>${row.skip_count.toLocaleString()}</td>
        <td class="${clsAmt(row.cumulative_usd)}">$${row.cumulative_usd.toFixed(4)}</td>
        <td>${row.avg_go_net_bps.toFixed(2)}</td>`;
      ptbody.appendChild(tr);
    }
    const otbody = document.querySelector('#opp-table tbody');
    otbody.innerHTML = '';
    for (const row of (opp.opportunities || [])) {
      const tr = document.createElement('tr');
      const cls = row.decision === 'GO' ? 'pos' : 'neg';
      tr.innerHTML = `
        <td class="meta">${(row.ts ?? '').slice(11,19)}</td>
        <td class="pair">${row.pair}</td>
        <td class="${cls}">${row.decision}</td>
        <td class="meta">${row.reason}</td>
        <td>${(row.expected_net_bps ?? 0).toFixed(2)}</td>
        <td class="${clsAmt(row.theoretical_pnl_usd)}">$${(row.theoretical_pnl_usd ?? 0).toFixed(4)}</td>`;
      otbody.appendChild(tr);
    }

    // Replay simulator card
    document.getElementById('sim-filled').textContent =
      (sim.n_filled ?? 0).toLocaleString() + '/' + (sim.n_trades ?? 0).toLocaleString();
    document.getElementById('sim-hit').textContent = ((sim.hit_rate ?? 0) * 100).toFixed(1) + '%';
    const scum = sim.cumulative_pnl_usd ?? 0;
    const scumEl = document.getElementById('sim-cum');
    scumEl.textContent = '$' + scum.toFixed(4);
    scumEl.className = 'stat-val ' + clsAmt(scum);
    document.getElementById('sim-sharpe').textContent = '—';  // populated by run replay endpoint result
    const sbody = document.querySelector('#sim-by-pair tbody');
    sbody.innerHTML = '';
    for (const row of (sim.by_pair || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="pair">${row.pair}</td>
        <td>${(row.n_filled || 0).toLocaleString()}</td>
        <td class="${clsAmt(row.cumulative_usd)}">$${row.cumulative_usd.toFixed(4)}</td>
        <td>${row.avg_realized_net_bps.toFixed(2)}</td>`;
      sbody.appendChild(tr);
    }

    // HistGBT card
    document.getElementById('m-loaded').textContent = model.loaded ? 'yes' : 'no';
    document.getElementById('m-auc').textContent = model.loaded ? (model.holdout_auc ?? 0).toFixed(3) : '—';
    document.getElementById('m-ntrain').textContent = model.loaded ? (model.n_train ?? 0).toLocaleString() : '—';
    document.getElementById('m-thr').textContent = model.loaded ? (model.veto_threshold ?? 0) : '—';

    // Soak summary
    const stbody = document.querySelector('#soak-tables tbody');
    stbody.innerHTML = '';
    for (const [name, info] of Object.entries(soak.tables || {})) {
      const tr = document.createElement('tr');
      tr.innerHTML = info
        ? `<td class="pair">${name}</td><td>${info.n.toLocaleString()}</td><td class="meta">${(info.last||'').slice(11,19)}</td>`
        : `<td class="pair">${name}</td><td class="meta" colspan="2">no data</td>`;
      stbody.appendChild(tr);
    }
    const dsbody = document.querySelector('#soak-spreads tbody');
    dsbody.innerHTML = '';
    for (const row of (soak.spread_distribution || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="pair">${row.pair}</td>
        <td>${row.n.toLocaleString()}</td>
        <td>${row.min.toFixed(2)}</td>
        <td>${row.median.toFixed(2)}</td>
        <td>${row.max.toFixed(2)}</td>`;
      dsbody.appendChild(tr);
    }
    const driftEl = document.getElementById('drift-log');
    driftEl.textContent = (soak.drift_alerts || []).slice(-5).join('\n') || '—';
  } catch (e) {
    console.error('refresh failed', e);
  }
}

async function setHalt(setIt) {
  await jpost('/api/arb/halt', {action: setIt ? 'set' : 'clear', reason: 'dashboard button'});
  refresh();
}

async function runDrill() {
  const btn = event.target; btn.disabled = true;
  document.getElementById('drill-result').innerHTML = 'running <span class="spinner"></span>';
  try {
    const r = await jpost('/api/arb/run_drill');
    document.getElementById('drill-result').innerHTML =
      `<span class="${r.passed ? 'pos' : 'neg'}">${r.n_passed}/${r.n_total} pass</span>`;
    document.getElementById('drill-checks').innerHTML =
      r.checks.map(c => `<div class="${c.ok ? 'ok' : 'bad'}">${c.ok ? '✓' : '✗'} ${c.name}<span class="meta"> ${c.detail || ''}</span></div>`).join('');
  } finally { btn.disabled = false; refresh(); }
}

async function runReplay() {
  const btn = document.getElementById('btn-replay'); btn.disabled = true;
  document.getElementById('replay-msg').innerHTML = 'running <span class="spinner"></span>';
  try {
    const r = await jpost('/api/arb/run_replay', {write: true});
    if (r.error) {
      document.getElementById('replay-msg').innerHTML =
        `<span class="neg">error: ${r.error}</span>`;
      return;
    }
    const sharpe = r.sharpe;
    document.getElementById('sim-sharpe').textContent =
      sharpe == null ? '—' : sharpe.toFixed(2);
    document.getElementById('replay-msg').innerHTML =
      `<span class="pos">${r.n_filled}/${r.n_go} filled · $${r.cumulative_pnl_usd.toFixed(4)} cum PnL · ${(r.avg_realized_net_bps).toFixed(2)} bps avg</span>`;
  } finally { btn.disabled = false; refresh(); }
}

async function runTrain() {
  const btn = document.getElementById('btn-train'); btn.disabled = true;
  document.getElementById('train-msg').innerHTML = 'training <span class="spinner"></span>';
  try {
    const r = await jpost('/api/arb/train_histgbt');
    if (r.error) {
      document.getElementById('train-msg').innerHTML =
        `<span class="neg">${r.error}: ${r.detail || ''}</span>`;
      return;
    }
    document.getElementById('train-msg').innerHTML =
      `<span class="pos">trained · AUC ${r.holdout_auc.toFixed(3)} · n=${r.n_train + r.n_holdout}</span>`;
  } finally { btn.disabled = false; refresh(); }
}

async function runCf() {
  const btn = document.getElementById('btn-cf'); btn.disabled = true;
  try {
    const fee = parseFloat(document.getElementById('cf-fee').value);
    const notional = parseFloat(document.getElementById('cf-notional').value);
    const r = await jpost('/api/arb/counterfactual',
                         {bybit_fee_bps: fee, notional_usd: notional});
    if (r.error) {
      document.getElementById('cf-go').textContent = 'err: ' + r.error;
      return;
    }
    document.getElementById('cf-go').textContent = (r.n_go || 0).toLocaleString();
    document.getElementById('cf-skip').textContent = (r.n_skip || 0).toLocaleString();
    document.getElementById('cf-pnl').textContent = '$' + (r.go_pnl_total || 0).toFixed(2);
    document.getElementById('cf-pnl').className = 'stat-val ' + clsAmt(r.go_pnl_total);
    const tbody = document.querySelector('#cf-by-pair tbody');
    tbody.innerHTML = '';
    for (const row of (r.by_pair || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="pair">${row.pair}</td>
        <td>${row.go.toLocaleString()}</td>
        <td>${row.skip.toLocaleString()}</td>
        <td class="${clsAmt(row.pnl)}">$${row.pnl.toFixed(2)}</td>
        <td>${row.max_net_bps.toFixed(2)}</td>`;
      tbody.appendChild(tr);
    }
  } finally { btn.disabled = false; }
}

async function toggleMaker(on) {
  await jpost('/api/arb/maker_mode', {enabled: on});
}

async function loadMakerInitial() {
  const r = await jget('/api/arb/maker_mode');
  document.getElementById('maker-toggle').checked = !!r.enabled;
}

refresh();
loadMakerInitial();
setInterval(refresh, 3000);
</script>
</body></html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(arb_bp)

    # P1-1 SAFETY (2026-05-11): require X-API-Key header on state-mutating
    # POST endpoints AND on sensitive GET endpoints when ARB_API_KEY is
    # configured. When the key is unset, log a startup warning but allow
    # all requests (localhost dev mode). Mirrors trading-bot dashboard pattern.
    #
    # POST-fix re-review NEW-4 (2026-05-11): /zombies + /correctness GETs
    # leak process info + endpoint topology. Added to gated path explicitly.
    GATED_GETS = {"/api/arb/zombies", "/api/arb/correctness",
                   "/api/arb/tft_eta", "/api/arb/maker_mode"}

    @app.before_request
    def _enforce_api_key():
        from flask import request, jsonify
        # Gate: every POST + the GATED_GETS list. Other GETs (spreads,
        # health, etc.) stay open for the dashboard UI.
        is_gated = (request.method != "GET") or (request.path in GATED_GETS)
        if not is_gated:
            return None
        api_key = os.environ.get("ARB_API_KEY")
        if not api_key:
            return None  # dev mode — no key configured, allow all
        provided = request.headers.get("X-API-Key", "")
        if provided != api_key:
            return jsonify({
                "error": "unauthorized",
                "detail": "Set X-API-Key header to match ARB_API_KEY env var",
            }), 401
        return None

    if not os.environ.get("ARB_API_KEY"):
        log.warning(
            "ARB_API_KEY not set -- dashboard POSTs are unauthenticated. "
            "Set ARB_API_KEY in .env for any exposed deployment."
        )

    @app.route("/")
    def index():
        return render_template_string(INDEX_HTML)

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True})

    return app


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="arbitrage_strategy standalone dashboard")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT_FALLBACK)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pid_file = config.PIDS_DIR / "dashboard.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    app = create_app()
    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    finally:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(cli_main())
