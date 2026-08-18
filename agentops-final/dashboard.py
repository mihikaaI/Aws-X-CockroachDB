"""Live explainability dashboard: tails the agent_trace table and renders
each agent step as it lands, in the browser, in real time.

    python dashboard.py            # serves on http://localhost:8888
    # ...then in another terminal run demo_scenario.py or the orchestrator loop

Stdlib only (http.server) -- no new dependencies. The page polls a tiny JSON
endpoint and renders *optimistically*: the instant a new incident appears it
lays down ghost cards for every step the pipeline is expected to take, then
reconciles each ghost to a confirmed card as the real trace row arrives from
CockroachDB. So the UI never sits blank waiting on the DB round-trip.
"""
import json
import os
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from tools import crdb_client

PORT = int(os.getenv("DASHBOARD_PORT", "8888"))

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentOps · Live Trace</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 0;
         background: #0b1020; color: #e6ecff; }
  header { padding: 18px 24px; border-bottom: 1px solid #223; display: flex;
           align-items: baseline; gap: 12px; }
  header h1 { font-size: 18px; margin: 0; }
  header .sub { color: #8ea3d6; font-size: 13px; }
  .incident { margin: 20px 24px; border: 1px solid #223; border-radius: 12px;
              overflow: hidden; }
  .incident > .hd { padding: 10px 16px; background: #131a33; font-weight: 600;
                    font-size: 13px; color: #a9bdf0; display:flex; justify-content:space-between;}
  .steps { display: flex; flex-wrap: wrap; gap: 12px; padding: 16px; }
  .card { min-width: 200px; flex: 1 1 200px; border: 1px solid #2a3358;
          border-radius: 10px; padding: 12px 14px; background: #10162e;
          transition: opacity .3s, border-color .3s, transform .3s; }
  .card .agent { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
                 color: #7fa0ff; }
  .card .step { font-weight: 600; margin: 2px 0 6px; }
  .card .detail { font-size: 13px; color: #b9c6ec; word-break: break-word; }
  .card.pending { opacity: .45; border-style: dashed; }
  .card.pending .detail::after { content: "waiting…"; color: #6f80ad; }
  .card.confirmed { border-color: #3ad29f; }
  .badge { font-size: 10px; padding: 1px 7px; border-radius: 999px; }
  .badge.pending { background:#2a2f4a; color:#8ea3d6; }
  .badge.confirmed { background:#12362b; color:#3ad29f; }
  .empty { color:#6f80ad; padding: 40px 24px; }
</style>
</head>
<body>
<header>
  <h1>AgentOps</h1>
  <span class="sub">live agent_trace · optimistic rendering · polling every 1s</span>
</header>
<div id="root"><div class="empty">Waiting for an incident… run <code>python demo_scenario.py</code>.</div></div>
<script>
// The pipeline is deterministic, so we know which steps to expect per incident.
// We render these optimistically the moment an incident's first row shows up.
const EXPECTED = [
  {agent:"monitor",    step:"latency check"},
  {agent:"memory",     step:"recall"},
  {agent:"diagnostic", step:"diagnosis"},
  {agent:"execution",  step:"fix applied"},
  {agent:"monitor",    step:"latency check"},   // re-benchmark
  {agent:"reporting",  step:"report"},
];

const incidents = new Map();  // incident_id -> {rows: [...]}

function render() {
  const root = document.getElementById("root");
  if (incidents.size === 0) return;
  root.innerHTML = "";
  // newest incident first
  const ids = [...incidents.keys()].reverse();
  for (const id of ids) {
    const { rows } = incidents.get(id);
    const el = document.createElement("div");
    el.className = "incident";
    const confirmedCount = rows.length;
    el.innerHTML = `<div class="hd"><span>incident ${id ? id.slice(0,8) : "—"}</span>
      <span>${confirmedCount} step(s) confirmed</span></div>`;
    const steps = document.createElement("div");
    steps.className = "steps";

    // Confirmed cards (real trace rows), then any expected-but-not-yet-arrived
    // steps as optimistic ghosts.
    const used = new Array(rows.length).fill(false);
    const cards = [];
    for (const exp of EXPECTED) {
      const idx = rows.findIndex((r, i) => !used[i] && r.agent_name === exp.agent && r.step === exp.step);
      if (idx >= 0) {
        used[idx] = true;
        cards.push(card(rows[idx].agent_name, rows[idx].step, rows[idx].detail, "confirmed"));
      } else {
        cards.push(card(exp.agent, exp.step, "", "pending"));
      }
    }
    // Any extra confirmed rows we didn't map to an expected slot (e.g. scaling).
    rows.forEach((r, i) => { if (!used[i]) cards.push(card(r.agent_name, r.step, r.detail, "confirmed")); });
    cards.forEach(c => steps.appendChild(c));
    el.appendChild(steps);
    root.appendChild(el);
  }
}

function card(agent, step, detail, state) {
  const d = document.createElement("div");
  d.className = "card " + state;
  d.innerHTML = `<div class="agent">${agent}
      <span class="badge ${state}">${state}</span></div>
    <div class="step">${escapeHtml(step)}</div>
    <div class="detail">${escapeHtml(detail || "")}</div>`;
  return d;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function poll() {
  try {
    const res = await fetch("/api/trace");
    const data = await res.json();
    incidents.clear();
    for (const row of data) {
      const key = row.incident_id || "unassigned";
      if (!incidents.has(key)) incidents.set(key, { rows: [] });
      incidents.get(key).rows.push(row);
    }
    render();
  } catch (e) { /* keep polling */ }
}
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


def _recent_trace(limit=300):
    rows = crdb_client.run_query(
        """SELECT incident_id, agent_name, step, detail, created_at
           FROM agent_trace
           ORDER BY created_at ASC
           LIMIT %s""",
        (limit,),
    )
    return rows or []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet the default access logging
        pass

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/trace":
            try:
                rows = _recent_trace()
                body = json.dumps(rows, default=_json_default).encode("utf-8")
                self._send(200, body, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"AgentOps dashboard on http://localhost:{PORT} (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
