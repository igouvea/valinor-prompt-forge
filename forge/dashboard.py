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
  #prog-summary { font-weight:400; color:var(--muted); text-transform:none; letter-spacing:0; font-size:12px; }
  .progbar { height:10px; background:#21262d; border-radius:5px; overflow:hidden; margin:4px 0 12px; }
  .progfill { height:100%; width:0%; background:var(--good); transition:width .4s ease; }
  .steps { display:grid; gap:3px; }
  .step { display:flex; align-items:center; gap:8px; font-size:13px; padding:3px 6px; border-radius:5px; }
  .step .ico { width:14px; text-align:center; }
  .step .lbl { flex:1; }
  .step .tps { color:var(--accent); font-variant-numeric:tabular-nums; width:84px; text-align:right; font-size:12px; }
  .step .t { color:var(--fg); font-variant-numeric:tabular-nums; width:64px; text-align:right; }
  .step .avg { color:var(--muted); opacity:.75; font-variant-numeric:tabular-nums; width:96px; text-align:right; font-size:12px; }
  .step.head { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); opacity:1; padding-bottom:2px; }
  .step.head .t, .step.head .tps, .step.head .avg, .step.head .lbl { color:var(--muted); }
  .step.done { opacity:.65; } .step.done .ico { color:var(--good); }
  .step.running { background:rgba(88,166,255,.10); } .step.running .ico { color:var(--accent); }
  .step.pending { opacity:.5; }
  .bench-row { display:flex; gap:10px; flex-wrap:wrap; }
  .bench { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:8px 12px; min-width:140px; }
  .bench .bn { font-size:12px; color:var(--muted); }
  .bench .bv { font-size:20px; font-weight:700; margin:2px 0; }
  .bench .bd { font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; }
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
  <div id="bench-strip" style="display:none; margin:0 0 16px"></div>
  <div class="chart" id="progress-panel" style="display:none">
    <h2>Loop progress <span id="prog-summary"></span></h2>
    <div class="progbar"><div id="prog-fill" class="progfill"></div></div>
    <div id="prog-steps" class="steps"></div>
  </div>
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
  if(!LAST){ el.textContent = ""; return; }
  const active = ["running","proposing","judging","starting"].includes(LAST.status);
  const drift = (active && LAST.updated_at) ? Math.max(0, Date.now()/1000 - LAST.updated_at) : 0;
  const p = LAST.progress;
  if(p && active){
    el.textContent = `● ${p.current||"working"} · elapsed ${fmtDur((p.elapsed||0)+drift)} · ETA ~${fmtDur(Math.max(0,(p.eta_seconds||0)-drift))}`;
    el.classList.toggle("stale", drift > 600);
  } else if(active){
    el.textContent = `● working · ${Math.round(drift)}s in this phase`;
    el.classList.remove("stale");
  } else {
    el.textContent = LAST.updated_at ? `updated ${Math.round(Date.now()/1000 - LAST.updated_at)}s ago` : "";
    el.classList.remove("stale");
  }
  renderProgress(LAST);
}

function fmt(n, d=3){ return (n==null||isNaN(n)) ? "–" : Number(n).toFixed(d); }
function fmtDur(s){ s=Math.max(0,Math.round(s||0)); if(s<60) return s+"s"; const m=Math.floor(s/60), r=s%60; return r? m+"m "+r+"s" : m+"m"; }

// Loop-progress panel: step checklist + bar + ETA. Interpolates elapsed/ETA
// from updated_at so the numbers tick smoothly between server updates.
function renderProgress(s){
  const panel = $("progress-panel"); const p = s && s.progress;
  if(!p){ panel.style.display="none"; return; }
  panel.style.display="";
  const active = ["running","proposing","judging","starting"].includes(s.status);
  const drift = (active && s.updated_at) ? Math.max(0, Date.now()/1000 - s.updated_at) : 0;
  const curEl = (p.current_elapsed||0)+drift;
  $("prog-summary").textContent = `${p.exp_id} · step ${p.steps_done}/${p.steps_total}`
    + (p.current ? ` · ${p.current} (${fmtDur(curEl)})` : "")
    + ` · elapsed ${fmtDur((p.elapsed||0)+drift)} · ETA ~${fmtDur(Math.max(0,(p.eta_seconds||0)-drift))}`;
  $("prog-fill").style.width = (p.steps_total ? (p.steps_done/p.steps_total*100) : 0) + "%";
  const head = `<div class="step head"><span class="ico"></span><span class="lbl">step</span>`
    + `<span class="tps">tok/s</span><span class="t">time</span><span class="avg">avg</span></div>`;
  $("prog-steps").innerHTML = head + (p.steps||[]).map(st=>{
    const ico = st.status==="done" ? "✓" : (st.status==="running" ? "▶" : "·");
    const dur = st.seconds ? fmtDur(st.seconds) : (st.status==="running" ? fmtDur(curEl) : "");
    const tps = (st.tps!=null) ? `${st.tps} tok/s` : (st.status==="running" ? "…" : "");
    const avg = st.avg ? `avg ${fmtDur(st.avg)}` : "";
    return `<div class="step ${st.status}"><span class="ico">${ico}</span>`
      + `<span class="lbl">${st.label}</span>`
      + `<span class="tps">${tps}</span><span class="t">${dur}</span><span class="avg">${avg}</span></div>`;
  }).join("");
}

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
  const val = e => (e.aggregate!=null ? e.aggregate : (e.score||0));  // aggregate in rotate mode
  const ys = hist.map(val);
  const maxY = Math.max(1, ...ys), n = hist.length;
  const X = i => pad + (n<=1?0:(i/(n-1))*(W-2*pad));
  const Y = v => H-pad - (v/maxY)*(H-2*pad);
  let path = "", dots = "";
  hist.forEach((e,i)=>{
    path += (i?"L":"M") + X(i).toFixed(1) + "," + Y(val(e)).toFixed(1) + " ";
    const col = e.adopted ? "#3fb950" : "#8b949e";
    dots += `<circle cx="${X(i).toFixed(1)}" cy="${Y(val(e)).toFixed(1)}" r="3" fill="${col}"/>`;
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

function renderBenchmarks(s){
  const el = $("bench-strip"); const b = s && s.benchmarks;
  if(!b || !Object.keys(b).length){ el.style.display="none"; return; }
  el.style.display="";
  el.innerHTML = `<div class="eyebrow" style="margin-bottom:6px">Per-benchmark champion · rotate mode</div>`
    + `<div class="bench-row">` + Object.entries(b).map(([name,v])=>
        `<div class="bench"><div class="bn">${name}</div><div class="bv">${fmt(v.score,3)}</div>`
        + `<div class="bd">t ${fmt(v.tests,2)} · s ${fmt(v.speed,2)} · r ${fmt(v.rubric,2)}</div></div>`
      ).join("") + `</div>`;
}

async function tick(){
  try{
    const s = await (await fetch("/api/live")).json();
    LAST = s;
    renderBadge(s); renderCfg(s.config); renderCards(s);
    renderBenchmarks(s); renderProgress(s);
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
