"""Bounded GET-only destination and delivery check. Never sends Discord messages."""
import json
import os
import re
import sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
import requests

TABLES={'robotics':('robotics_job_notifications','state'),
        'tech':('tech_job_notifications','state'),
        'teams':('team_job_notifications','status')}

def get(url,**kwargs):
    try:
        r=requests.get(url,timeout=20,**kwargs)
        if r.status_code!=200:
            return None,{'http_status':r.status_code}
        return r.json(),{'http_status':200}
    except Exception as exc:
        return None,{'error_type':type(exc).__name__}

def audit(module):
    row={'module':module}
    hook=os.environ.get('AUDIT_WEBHOOK_'+module.upper(),'').strip()
    row['configured']=bool(hook)
    if not hook:return row
    parts=urlsplit(hook)
    row['destination_host']=parts.hostname
    if parts.hostname not in {'discord.com','discordapp.com','canary.discord.com'}:
        row['destination_metadata']='Configured relay; direct Discord metadata not inspected'
        return row
    assert re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+/?',parts.path)
    clean=urlunsplit((parts.scheme,parts.netloc,parts.path.rstrip('/'),'',''))
    meta,result=get(clean)
    row['metadata_request']=result
    if meta:
        row['webhook']={k:meta.get(k) for k in ('id','name','channel_id','guild_id','type')}
        row['channel_url']='https://discord.com/channels/'+str(meta.get('guild_id'))+'/'+str(meta.get('channel_id'))
    base=os.environ.get('SUPABASE_URL','').rstrip('/')
    key=os.environ.get('SUPABASE_SERVICE_ROLE_KEY','')
    if not base or not key:return row
    table,state=TABLES[module]
    headers={'apikey':key,'Authorization':'Bearer '+key}
    columns=state+',delivered_at'+(',discord_message_id' if module!='teams' else '')
    data,status=get(base+'/rest/v1/'+table,headers=headers,params={
        'select':columns,state:'eq.delivered','order':'delivered_at.desc.nullslast','limit':10})
    row['delivery_query']=status
    if data is not None:
        row['latest_delivery_records']=data
        row['last_confirmed_delivery']=max((r.get('delivered_at') or '' for r in data),default=None)
        ids=list(dict.fromkeys(r.get('discord_message_id') for r in data if r.get('discord_message_id')))[:2]
        row['message_readbacks']=[]
        for message_id in ids:
            message,observed=get(clean+'/messages/'+str(message_id))
            if message:
                observed.update({k:message.get(k) for k in ('id','channel_id','webhook_id','timestamp')})
                observed['embed_count']=len(message.get('embeds',[]))
                observed['channel_matches']=message.get('channel_id')==meta.get('channel_id') if meta else None
            row['message_readbacks'].append(observed)
    return row

if __name__=='__main__':
    modules=sys.argv[1:]
    assert modules and len(modules)<=3 and all(m in TABLES for m in modules)
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'get_only':True,
            'messages_sent':0,'modules':[audit(m) for m in modules]}
    Path('discord-wiring-audit.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))
