"""
forge.dashboard

A small FastAPI GUI that visualizes the autoresearch loop live. It reads the
state written by the orchestrator (state/live.json + state/experiments.jsonl +
state/runs/) — it never drives the loop itself, so you can open it any time,
during or after a run.

    uv run forge dashboard   →  http://127.0.0.1:7777

Shows: current status/phase, the champion + its score breakdown, score-over-time
chart, run totals/cost, and an expandable per-experiment table (hypothesis,
rationale, verdicts). Polls every 2s.
"""

from __future__ import annotations

import json

from .config import CONFIG
from . import state


def _build_app():
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "[forge] dashboard needs fastapi + uvicorn. Run `uv sync`."
        ) from e

    app = FastAPI(title="valinor-prompt-forge")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _HTML

    @app.get("/api/live")
    def live() -> JSONResponse:
        return JSONResponse(state.read_live() or {"status": "idle", "status_detail": "no run yet"})

    @app.get("/api/journal")
    def journal() -> JSONResponse:
        return JSONResponse({"entries": state.read_journal()})

    @app.get("/api/experiment/{exp_id}")
    def experiment(exp_id: str) -> JSONResponse:
        run_dir = CONFIG.runs_dir / exp_id
        out: dict = {"exp_id": exp_id}
        result = run_dir / "result.json"
        if result.exists():
            try:
                out["result"] = json.loads(result.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                out["result"] = None
        rationale = run_dir / "candidate" / "RATIONALE.md"
        if rationale.exists():
            out["rationale"] = rationale.read_text(encoding="utf-8")
        return JSONResponse(out)

    return app


def main(argv: list[str] | None = None) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("[forge] dashboard needs uvicorn. Run `uv sync`.")
        return 2
    app = _build_app()
    url = f"http://{CONFIG.dashboard_host}:{CONFIG.dashboard_port}"
    print(f"[forge] dashboard → {url}", flush=True)
    uvicorn.run(app, host=CONFIG.dashboard_host, port=CONFIG.dashboard_port, log_level="warning")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Single-file UI (vanilla JS, polls the API). Same-origin relative fetches, so
# no host/port templating needed.
# ─────────────────────────────────────────────────────────────────────────────

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>valinor-prompt-forge</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e;
    --accent:#58a6ff; --good:#3fb950; --bad:#f85149; --warn:#d29922;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--border); display:flex;
           align-items:center; gap:16px; flex-wrap:wrap; }
  h1 { font-size:18px; margin:0; font-weight:600; letter-spacing:.2px; }
  h1 small { color:var(--muted); font-weight:400; font-size:13px; }
  .badge { padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600;
           text-transform:uppercase; letter-spacing:.5px; }
  .b-running { background:rgba(88,166,255,.15); color:var(--accent); }
  .b-proposing { background:rgba(210,153,34,.15); color:var(--warn); }
  .b-judging { background:rgba(210,153,34,.15); color:var(--warn); }
  .b-stopped, .b-idle { background:rgba(139,148,158,.15); color:var(--muted); }
  .b-error { background:rgba(248,81,73,.15); color:var(--bad); }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.4;} }
  .b-running, .b-proposing, .b-judging { animation:pulse 1.4s ease-in-out infinite; }
  .detail { color:var(--muted); font-size:13px; }
  #elapsed { font-variant-numeric:tabular-nums; }
  #elapsed.stale { color:var(--bad); }
  main { padding:24px; max-width:1100px; margin:0 auto; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:24px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }
  .card .k { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.5px; }
  .card .v { font-size:26px; font-weight:700; margin-top:4px; }
  .card .v small { font-size:13px; color:var(--muted); font-weight:400; }
  .bars { margin-top:10px; }
  .bar { display:flex; align-items:center; gap:8px; margin:3px 0; font-size:12px; }
  .bar .lbl { width:52px; color:var(--muted); }
  .bar .track { flex:1; background:#21262d; border-radius:4px; height:8px; overflow:hidden; }
  .bar .fill { height:100%; background:var(--accent); }
  .chart { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:24px; }
  .chart h2, .table-wrap h2 { font-size:14px; margin:0 0 12px; color:var(--muted);
                              text-transform:uppercase; letter-spacing:.5px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; font-size:12px; }
  tr.exp { cursor:pointer; }
  tr.exp:hover { background:rgba(88,166,255,.06); }
  .pill { font-weight:700; }
  .adopt { color:var(--good); } .disc { color:var(--muted); }
  .num { font-variant-numeric:tabular-nums; }
  tr.detailrow td { background:#0b0f14; color:var(--muted); white-space:pre-wrap;
                    font:12px ui-monospace,SFMono-Regular,Menlo,monospace; }
  .foot { color:var(--muted); font-size:12px; margin-top:16px; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>valinor-prompt-forge <small id="cfg"></small></h1>
  <span id="badge" class="badge b-idle">idle</span>
  <span id="detail" class="detail"></span>
  <span id="elapsed" class="detail"></span>
</header>
<main>
  <div class="grid" id="cards"></div>
  <div class="chart">
    <h2>Score over experiments</h2>
    <svg id="chart" width="100%" height="160" preserveAspectRatio="none"></svg>
  </div>
  <div class="table-wrap">
    <h2>Experiments</h2>
    <table>
      <thead><tr>
        <th>exp</th><th>score</th><th>tests</th><th>speed</th><th>rubric</th>
        <th>adopted</th><th>hypothesis</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="foot" id="foot"></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
let openRow = null;
let LAST = null;  // most recent live snapshot, for the client-side clock

// Ticks every second independent of server updates, so a long-running phase
// (where live.json's timestamp is frozen between transitions) still visibly
// counts up and is clearly alive — not "stuck".
function updateClock(){
  const el = $("elapsed");
  if(!LAST || !LAST.updated_at){ el.textContent = ""; return; }
  const age = Math.round(Date.now()/1000 - LAST.updated_at);
  const active = ["running","proposing","judging","starting"].includes(LAST.status);
  if(active){
    el.textContent = `● working · ${age}s in this phase`;
    el.classList.toggle("stale", age > 600);
  } else {
    el.textContent = LAST.updated_at ? `updated ${age}s ago` : "";
    el.classList.remove("stale");
  }
}

function fmt(n, d=3){ return (n==null||isNaN(n)) ? "–" : Number(n).toFixed(d); }

function renderBadge(s){
  const b = $("badge");
  b.textContent = s.status || "idle";
  b.className = "badge b-" + (s.status || "idle");
  $("detail").textContent = s.status_detail || "";
}

function renderCfg(c){
  if(!c) return;
  const w = c.weights || {};
  const ws = (w.speed != null ? w.speed : w.cycles);
  $("cfg").textContent = `· ${c.agent_cli}/${c.agent_model} · researcher ${c.researcher_model} `
    + `· w(t${w.tests} s${ws} r${w.rubric}) · [${(c.benchmarks||[]).join(", ")}]`;
}

function bar(lbl, val){
  const pct = Math.max(0, Math.min(1, val||0))*100;
  return `<div class="bar"><span class="lbl">${lbl}</span>`
    + `<span class="track"><span class="fill" style="width:${pct}%"></span></span>`
    + `<span class="num">${fmt(val,2)}</span></div>`;
}

function renderCards(s){
  const champ = s.champion || {};
  const bd = champ.breakdown || {};
  const t = s.totals || {};
  $("cards").innerHTML = `
    <div class="card">
      <div class="k">Champion</div>
      <div class="v">${fmt(champ.score)} <small>${champ.exp_id||"—"}</small></div>
      <div class="bars">${bar("tests",bd.tests)}${bar("speed",(bd.speed!=null?bd.speed:bd.cycles))}${bar("rubric",bd.rubric)}</div>
    </div>
    <div class="card"><div class="k">Best score</div><div class="v">${fmt(s.best_score)}</div>
      <div class="detail">plateau: ${s.plateau_count||0} / ${(s.config||{}).stop_plateau||"–"}</div></div>
    <div class="card"><div class="k">Experiments</div>
      <div class="v">${t.experiments||0} <small>${t.adopted||0} adopted</small></div></div>
    <div class="card"><div class="k">Cost (≈USD)</div><div class="v">$${fmt(t.cost_usd,2)}</div>
      ${s.current_exp ? `<div class="detail">+ $${fmt(s.current_exp.cost_usd,2)} this run · ${s.current_exp.roles_done||0} roles done</div>` : ""}</div>`;
}

function renderChart(hist){
  const svg = $("chart");
  const W = svg.clientWidth || 900, H = 160, pad = 24;
  if(!hist || !hist.length){ svg.innerHTML = ""; return; }
  const xs = hist.map((_,i)=>i), ys = hist.map(e=>e.score||0);
  const maxY = Math.max(1, ...ys), n = hist.length;
  const X = i => pad + (n<=1?0:(i/(n-1))*(W-2*pad));
  const Y = v => H-pad - (v/maxY)*(H-2*pad);
  let path = "", dots = "";
  hist.forEach((e,i)=>{
    path += (i?"L":"M") + X(i).toFixed(1) + "," + Y(e.score||0).toFixed(1) + " ";
    const col = e.adopted ? "#3fb950" : "#8b949e";
    dots += `<circle cx="${X(i).toFixed(1)}" cy="${Y(e.score||0).toFixed(1)}" r="3" fill="${col}"/>`;
  });
  svg.innerHTML =
    `<line x1="${pad}" y1="${H-pad}" x2="${W-pad}" y2="${H-pad}" stroke="#30363d"/>`
    + `<path d="${path}" fill="none" stroke="#58a6ff" stroke-width="2"/>` + dots;
}

function renderRows(hist){
  const rows = $("rows");
  rows.innerHTML = "";
  [...hist].reverse().forEach(e=>{
    const bd = e.breakdown || {};
    const tr = document.createElement("tr");
    tr.className = "exp";
    tr.innerHTML =
      `<td>${e.exp_id}</td><td class="num pill">${fmt(e.score)}</td>`
      + `<td class="num">${fmt(bd.tests,2)}</td><td class="num">${fmt((bd.speed!=null?bd.speed:bd.cycles),2)}</td>`
      + `<td class="num">${fmt(bd.rubric,2)}</td>`
      + `<td class="${e.adopted?'adopt':'disc'}">${e.adopted?'✓ adopted':'·'}</td>`
      + `<td>${(e.hypothesis||"").slice(0,90)}</td>`;
    tr.onclick = ()=>toggleDetail(tr, e.exp_id);
    rows.appendChild(tr);
  });
}

async function toggleDetail(tr, expId){
  if(tr.nextSibling && tr.nextSibling.classList && tr.nextSibling.classList.contains("detailrow")){
    tr.nextSibling.remove(); return;
  }
  document.querySelectorAll(".detailrow").forEach(r=>r.remove());
  const det = document.createElement("tr");
  det.className = "detailrow";
  det.innerHTML = `<td colspan="7">loading ${expId}…</td>`;
  tr.after(det);
  try{
    const r = await fetch("/api/experiment/"+expId); const d = await r.json();
    let txt = "";
    if(d.rationale) txt += d.rationale + "\\n\\n";
    if(d.result){
      (d.result.benchmarks||[]).forEach(b=>{
        txt += `[${b.benchmark}] verdict=${b.verdict} tests=${b.test.passed}/${b.test.total} `
          + `time=${fmt(b.wall_seconds,0)}s cost=$${fmt(b.cost_usd,2)}\\n`;
      });
    }
    det.innerHTML = `<td colspan="7">${(txt||"(no detail)").replace(/</g,"&lt;")}</td>`;
  }catch(err){ det.innerHTML = `<td colspan="7">error: ${err}</td>`; }
}

async function tick(){
  try{
    const s = await (await fetch("/api/live")).json();
    LAST = s;
    renderBadge(s); renderCfg(s.config); renderCards(s);
    renderChart(s.history||[]); renderRows(s.history||[]);
    const u = s.updated_at ? new Date(s.updated_at*1000).toLocaleTimeString() : "";
    $("foot").textContent = "last update " + u;
    updateClock();
  }catch(e){ $("detail").textContent = "dashboard offline: " + e; }
}
tick(); setInterval(tick, 2000); setInterval(updateClock, 1000);
</script>
</body>
</html>
"""
