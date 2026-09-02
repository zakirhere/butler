from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from butler.auth import require_token
from butler.service_status import collect_status

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return PAGE


@router.get("/dashboard/data", dependencies=[Depends(require_token)])
async def dashboard_data() -> dict:
    return collect_status()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Butler Services</title>
<style>
:root{color-scheme:dark;--bg:#101316;--card:#191e23;--line:#303840;--muted:#9aa5af;--good:#55d187;--warn:#f4bd57;--bad:#ff7474}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf1f4;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1280px;margin:auto;padding:36px 24px}h1{margin:0 0 8px;font-size:30px}p{color:var(--muted)}.toolbar{display:flex;gap:12px;align-items:center;margin:24px 0}.summary{display:flex;gap:10px;flex-wrap:wrap}.pill{padding:7px 12px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
button{background:#2e78d0;color:white;border:0;border-radius:7px;padding:9px 14px;font-weight:600;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}.head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.name{font:600 16px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}.status{font-size:12px;padding:4px 8px;border-radius:999px;white-space:nowrap}.running{color:var(--good);background:#123c28}.idle{color:var(--warn);background:#493813}.not-loaded,.not-installed{color:var(--muted);background:#293039}.meta{display:grid;grid-template-columns:100px 1fr;gap:7px;margin:17px 0;color:var(--muted)}.meta b{color:#edf1f4;font-weight:500}.activity{border-top:1px solid var(--line);padding-top:13px;color:var(--muted);font-size:13px;line-height:1.5}.activity code{display:block;color:#d9e0e6;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:5px}.error{color:var(--bad)}.error code{color:#ffb0b0}.empty{padding:30px;text-align:center;color:var(--muted)}
</style></head><body><main><h1>Butler services</h1><p>Read-only launchd status and recent generated messages.</p><div class="toolbar"><button id="refresh">Refresh</button><span id="updated" class="pill">Not loaded</span><div id="summary" class="summary"></div></div><section id="grid" class="grid"><div class="empty">Enter your Butler token to load status.</div></section></main>
<script>
const esc=s=>String(s??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
let token=localStorage.getItem("butler-token")||prompt("Butler token");if(token)localStorage.setItem("butler-token",token);
async function load(){if(!token)return;const r=await fetch("/dashboard/data",{headers:{Authorization:"Bearer "+token}});if(r.status===401){localStorage.removeItem("butler-token");token=prompt("Invalid token. Enter Butler token");if(token){localStorage.setItem("butler-token",token);return load()}return}render(await r.json())}
function render(d){const c=d.services.reduce((a,s)=>(a[s.status]=(a[s.status]||0)+1,a),{});updated.textContent="Updated "+new Date(d.generated_at).toLocaleString();summary.innerHTML=Object.entries(c).map(([k,v])=>`<span class="pill">${v} ${esc(k)}</span>`).join("");grid.innerHTML=d.services.map(s=>`<article class="card"><div class="head"><div class="name">${esc(s.label)}</div><span class="status ${esc(s.status)}">${esc(s.status)}</span></div><div class="meta"><span>Schedule</span><b>${esc(s.schedule)}</b><span>PID</span><b>${esc(s.pid??"—")}</b><span>Browser</span><b>${esc(s.browser)}</b><span>Command</span><b>${esc(s.command)}</b></div><div class="activity ${s.log.health==="error"?"error":""}">Recent activity · ${esc(s.log.timestamp||"unknown")}<code>${esc(s.log.message)}</code></div></article>`).join("")}
refresh.addEventListener("click",load);load();setInterval(load,30000);
</script></body></html>"""
