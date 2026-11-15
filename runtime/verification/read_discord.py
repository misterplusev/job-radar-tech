"""Read existing Discord deliveries only: no POST, no new messages or DB writes."""
import datetime as dt
import json
import os
from pathlib import Path
import requests

if os.environ.get('GITHUB_ACTIONS') != 'true':
    raise SystemExit('Cloud CI only')
base = os.environ['SUPABASE_URL'].rstrip('/')
key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
auth = {'apikey':key, 'Authorization':'Bearer '+key}
definitions = [('robotics','robotics_job_notifications','DISCORD_WEBHOOK_JOBS'),
               ('teams','team_job_notifications','DISCORD_WEBHOOK_TEAMS'),
               ('sportsbook','sportsbook_job_notifications','DISCORD_WEBHOOK_SPORTSBOOK')]
if os.environ.get('PROOF_MODULE') == 'tech':
    definitions = [('tech','tech_job_notifications','DISCORD_WEBHOOK_JOBS')]
report = {'observed_at':dt.datetime.now(dt.timezone.utc).isoformat(), 'messages_sent':0, 'modules':[]}
failed = False
for module, table, variable in definitions:
    item = {'module':module}
    try:
        hook = os.environ[variable].strip().split('?')[0].rstrip('/')
        response = requests.get(base+'/rest/v1/'+table, headers=auth, params={
            'select':'discord_message_id,delivered_at', 'discord_message_id':'not.is.null',
            'order':'delivered_at.desc', 'limit':1}, timeout=25)
        response.raise_for_status()
        row = response.json()[0]
        meta = requests.get(hook, timeout=20)
        item['metadata_http'] = meta.status_code
        meta.raise_for_status()
        metadata = meta.json()
        found = requests.get(hook+'/messages/'+row['discord_message_id'], timeout=20)
        item['message_http'] = found.status_code
        found.raise_for_status()
        message = found.json()
        assert message['channel_id'] == metadata['channel_id']
        assert message['webhook_id'] == metadata['id']
        item.update(message_id=message['id'], channel_id=message['channel_id'],
                    timestamp=message['timestamp'], embeds=len(message.get('embeds', [])),
                    url=f"https://discord.com/channels/{metadata['guild_id']}/{message['channel_id']}/{message['id']}",
                    passed=True)
    except Exception as exc:
        # Never serialize exception text: requests errors can contain a webhook token.
        item.update(passed=False, error_type=type(exc).__name__)
        failed = True
    report['modules'].append(item)
Path('proof').mkdir(exist_ok=True)
Path('proof/discord-readback.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
raise SystemExit(1 if failed else 0)
