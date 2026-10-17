"""Render a self-contained, filterable HTML dashboard from scored jobs.

Works with either storage backend (normalizes the row shape). No server needed —
just open the output file.
"""
from __future__ import annotations

import json
from pathlib import Path


def _normalize(rows):
    out = []
    for r in rows:
        job = r if "company" in r else (r.get("jobs") or {})
        out.append({
            "score": r.get("fit_score"),
            "archetype": r.get("archetype") or "",
            "company": job.get("company") or "",
            "title": job.get("title") or "",
            "location": job.get("location") or "",
            "url": job.get("url") or "",
            "source": job.get("source") or "",
            "rationale": (r.get("rationale") or "")[:240],
            "posted_at": job.get("posted_at") or "",
        })
    return out


def _from_active(rows):
    """Unscored shape — the module pipeline never writes job_scores."""
    return [{
        "score": None,
        "archetype": "",
        "company": r.get("company") or "",
        "title": r.get("title") or "",
        "location": r.get("location") or "",
        "url": r.get("url") or "",
        "source": r.get("source") or "",
        "rationale": "",
        "posted_at": r.get("posted_at") or "",
    } for r in rows]


def render_dashboard(settings, store, out_path: str, limit: int = 500) -> str:
    rows = _from_active(store.recent_active_jobs(limit))
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    has_score = any(r["score"] is not None for r in rows)
    p.write_text(
        _HTML.replace("__DATA__", json.dumps(rows))
             .replace("__HAS_SCORE__", "true" if has_score else "false"),
        encoding="utf-8",
    )
    return str(p)


_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Job Radar</title><style>
:root{color-scheme:dark}body{margin:0;font:14px system-ui;background:#050506;color:#e8e8e8}
header{padding:16px 20px;border-bottom:1px solid #222}h1{margin:0;font-size:20px}
.controls{display:flex;gap:10px;flex-wrap:wrap;padding:14px 20px;position:sticky;top:0;background:#050506;border-bottom:1px solid #1a1a1a}
input,select{background:#0c0c0e;color:#eee;border:1px solid #2a2a2a;border-radius:8px;padding:8px}
table{width:100%;border-collapse:collapse}td,th{padding:8px 12px;border-bottom:1px solid #161616;text-align:left;vertical-align:top}
th{position:sticky;top:64px;background:#0a0a0b;cursor:pointer}
.score{font-weight:800;text-align:center;width:44px}
a{color:#7aa2ff;text-decoration:none}.muted{color:#8a8a8a;font-size:12px}
.pill{font-size:11px;padding:2px 7px;border:1px solid #2a2a2a;border-radius:999px;color:#bcbcbc}
</style></head><body>
<header><h1>Job Radar</h1><div class=muted id=count></div></header>
<div class=controls>
  <input id=q placeholder="search title / company / location" size=34>
  <label>min score <input id=min type=number value=70 min=0 max=100 style=width:70px></label>
  <select id=src></select>
  <label><input type=checkbox id=remote> remote only</label>
</div>
<table><thead><tr><th data-k=score>Fit</th><th data-k=title>Title</th><th data-k=company>Company</th><th data-k=location>Location</th><th data-k=source>Source</th></tr></thead><tbody id=tb></tbody></table>
<script>
const DATA=__DATA__,HAS_SCORE=__HAS_SCORE__;let sortK=HAS_SCORE?"score":"posted_at",asc=false;
const tb=document.getElementById("tb"),q=document.getElementById("q"),mn=document.getElementById("min"),
 src=document.getElementById("src"),rem=document.getElementById("remote"),cnt=document.getElementById("count");
if(!HAS_SCORE){mn.disabled=true;mn.title="no fit scores in this module"}
const sources=[...new Set(DATA.map(d=>d.source).filter(Boolean))].sort();
src.innerHTML="<option value=''>all sources</option>"+sources.map(s=>`<option>${s}</option>`).join("");
function color(s){return s>=85?"#6ee7b7":s>=70?"#fcd34d":"#9aa0a6"}
function render(){let r=DATA.filter(d=>{
 if(HAS_SCORE&&d.score!=null&&d.score<+mn.value)return false;
 if(src.value&&d.source!==src.value)return false;
 if(rem.checked&&!/remote/i.test(d.location||""))return false;
 const t=(q.value||"").toLowerCase();
 return !t||`${d.title} ${d.company} ${d.location||""}`.toLowerCase().includes(t);});
 r.sort((a,b)=>{const x=a[sortK],y=b[sortK];
  if(x==null)return 1;if(y==null)return -1;
  const c=(""+x).localeCompare(""+y,undefined,{numeric:true});return asc?c:-c;});
 cnt.textContent=r.length+" jobs";
 tb.innerHTML=r.map(d=>`<tr><td class=score style="color:${d.score==null?"#8a8a8a":color(d.score)}">${d.score==null?"-":d.score}</td>
 <td><a href="${d.url}" target=_blank>${d.title}</a>${d.rationale?`<div class=muted>${d.rationale}</div>`:""}</td>
 <td>${d.company}</td><td>${d.location||""}</td><td><span class=pill>${d.source||""}</span></td></tr>`).join("");}
document.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(k===sortK)asc=!asc;else{sortK=k;asc=false}render()});
[q,mn,src,rem].forEach(el=>el.addEventListener("input",render));render();
</script></body></html>"""
