"""
Standalone Flask app for the arbitrage dashboard.

Default port: config.DASHBOARD_PORT_FALLBACK (5001).

Per Q5: the canonical plan is to mount arb_blueprint into the sister
trading-bot Flask app at port 5000. This standalone runner is the Q5
fallback path and the Phase 1 default — it lets us validate the API
without touching the live bot's process.

Run:
  python -m src.dashboard.app_arb [--port 5001] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import logging
import os

from flask import Flask, jsonify, render_template_string

from src.dashboard.arb_blueprint import bp as arb_bp
from src.utils import config

log = logging.getLogger(__name__)


INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>arbitrage_strategy — Phase 1</title>
<style>
  body { font-family: ui-monospace, "Cascadia Code", "Consolas", monospace;
         background: #0d1117; color: #e6edf3; padding: 20px; }
  h1 { color: #58a6ff; font-size: 18px; }
  table { border-collapse: collapse; margin: 12px 0; }
  th, td { padding: 6px 12px; border: 1px solid #30363d; text-align: right; }
  th { background: #161b22; color: #7ee787; }
  td.pair { text-align: left; color: #d2a8ff; }
  .pos { color: #7ee787; }
  .neg { color: #ff7b72; }
  .meta { color: #8b949e; font-size: 12px; }
  pre { background: #161b22; padding: 8px; border: 1px solid #30363d;
        font-size: 11px; overflow-x: auto; }
  button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
           padding: 6px 12px; cursor: pointer; }
  button:hover { background: #30363d; }
</style>
</head><body>
<h1>arbitrage_strategy — Phase 1 dashboard</h1>
<p class="meta">Mode: <span id="mode">?</span> &middot;
   Ingestion: <span id="ingest">?</span> &middot;
   HALT: <span id="halt">?</span> &middot;
   Gas: <span id="gas">?</span> gwei</p>
<button onclick="refresh()">refresh</button>
<table id="spread-table">
  <thead><tr>
    <th class="pair">pair</th><th>bybit_mid</th><th>dex_mid</th>
    <th>spread (bps)</th><th>bybit_ts</th>
  </tr></thead>
  <tbody></tbody>
</table>
<h2 class="meta">raw /api/arb/spread</h2>
<pre id="raw"></pre>
<script>
async function refresh() {
  const [h, s, g] = await Promise.all([
    fetch('/api/arb/health').then(r => r.json()),
    fetch('/api/arb/spread').then(r => r.json()),
    fetch('/api/arb/gas').then(r => r.json()),
  ]);
  document.getElementById('mode').textContent = h.mode;
  document.getElementById('ingest').textContent =
    h.ingestion_running ? `running (pid ${h.ingestion_pid})` : 'NOT RUNNING';
  document.getElementById('halt').textContent = h.halt_active ? 'YES' : 'no';
  document.getElementById('gas').textContent =
    g.gas ? g.gas.total_gas_price_gwei.toFixed(4) : '?';
  const tbody = document.querySelector('#spread-table tbody');
  tbody.innerHTML = '';
  for (const row of (s.spreads || [])) {
    const tr = document.createElement('tr');
    const cls = row.spread_bps == null ? '' :
                (row.spread_bps > 0 ? 'pos' : 'neg');
    tr.innerHTML = `
      <td class="pair">${row.pair}</td>
      <td>${row.bybit_mid?.toLocaleString() ?? '?'}</td>
      <td>${row.dex_mid?.toLocaleString() ?? '?'}</td>
      <td class="${cls}">${row.spread_bps ?? '?'}</td>
      <td class="meta">${(row.bybit_ts ?? '').slice(11,19)}</td>`;
    tbody.appendChild(tr);
  }
  document.getElementById('raw').textContent = JSON.stringify(s, null, 2);
}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(arb_bp)

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
