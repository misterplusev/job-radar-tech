"""Read a real acknowledged job alert from each configured Discord channel without sending."""
import json, os
from pathlib import Path
from urllib.parse import urlsplit
import requests
module=os.environ["MODULE"]
table={"robotics":"robotics_job_notifications","tech":"tech_job_notifications","sports_teams":"team_job_notifications"}[module]
state,url=("status","url") if module=="sports_teams" else ("state","job_url")
base=os.environ["SUPABASE_URL"].rstrip("/")
hook=os.environ["JOB_WEBHOOK"].rstrip("/")
assert urlsplit(hook).hostname in {"discord.com","discordapp.com"} and urlsplit(hook).path.startswith("/api/webhooks/")
def get(url,**kwargs):
    try:r=requests.get(url,timeout=20,allow_redirects=False,**kwargs)
    except requests.RequestException as exc:raise RuntimeError(type(exc).__name__) from None
    if r.status_code!=200:raise RuntimeError(f"Receipt lookup returned HTTP {r.status_code}")
    return r.json()
key=os.environ["SUPABASE_SERVICE_ROLE_KEY"]
rows=get(base+"/rest/v1/"+table,headers={"apikey":key,"Authorization":"Bearer "+key},params={
    state:"eq.delivered","discord_message_id":"not.is.null","select":"discord_message_id,"+url,"order":"delivered_at.desc.nullslast","limit":12})
assert rows,"No confirmed delivery receipts"
webhook=get(hook)
out=[]
for mid in dict.fromkeys(str(r["discord_message_id"]) for r in rows):
    if not mid.isdecimal():continue
    message=get(hook+"/messages/"+mid)
    assert str(message["channel_id"])==str(webhook["channel_id"]) and str(message["webhook_id"])==str(webhook["id"])
    found={e["url"] for e in message.get("embeds",[]) if e.get("url")}
    matching=[r[url] for r in rows if str(r['discord_message_id'])==mid and r[url] in found]
    assert matching,"Stored jobs missing from the real Discord message"
    out.append({"module":module,"http":200,"confirmed_at":message.get("timestamp"),"matching_urls_in_sample":len(matching),
        "url":f'https://discord.com/channels/{webhook["guild_id"]}/{webhook["channel_id"]}/{mid}'})
    break
assert out
Path('discord-'+module+'.json').write_text(json.dumps(out[0],indent=2));print(json.dumps(out[0]))
