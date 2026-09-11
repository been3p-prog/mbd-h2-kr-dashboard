"""Explicit, hash-guarded deployment of only the metric adapters. No auth writes.

Run with --candidate-dir containing reviewed copies of the three runtime files.
Default is read-only preflight; --deploy separately authorizes file installation.
Service restarts and Slack tests are deliberately not part of this installer.
"""
import argparse
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

HOST='cnc-media@192.168.7.238'
KEY='/Users/sb.lee/.ssh/id_ed25519_mbd_server'
ROOT='/Users/cnc-media/services/'
SOURCES=[
 ('live-commerce-bot/app/scripts/answer_engine.py','live_answer_engine.py','da9f5d7ca2983479be2d655058c45e91691923e190becd7b6db3160e4055848f'),
 ('youtube-copilot/youtube_copilot/app.py','youtube_app.py','63acd21495c442b8d1bfe463fe6648860dcd167db52102828c85441ddc5aa1a9'),
 ('ad-booking-admin/scripts/slack-booking-bot.mjs','slack-booking-bot.mjs','cde2c382554f310797a11b397afd9f1ea65622f60df6df27d6da3ded3074d3f3'),
 ('live-commerce-bot/app/scripts/bot_metrics_adapter.py','bot_metrics_adapter.py',None),
 ('youtube-copilot/youtube_copilot/bot_metrics_adapter.py','bot_metrics_adapter.py',None),
 ('ad-booking-admin/scripts/bot-metrics-adapter.mjs','bot-metrics-adapter.mjs',None),
 ('mbd-dashboard-metrics/dashboard_metrics_client.py','dashboard_metrics_client.py',None),
]
REMOTE=r'''
import base64,datetime,hashlib,json,os,shutil,sys
from pathlib import Path
x=json.load(sys.stdin)
allowed={
'live-commerce-bot/app/scripts/answer_engine.py',
'youtube-copilot/youtube_copilot/app.py',
'ad-booking-admin/scripts/slack-booking-bot.mjs',
'live-commerce-bot/app/scripts/bot_metrics_adapter.py',
'youtube-copilot/youtube_copilot/bot_metrics_adapter.py',
'ad-booking-admin/scripts/bot-metrics-adapter.mjs',
'mbd-dashboard-metrics/dashboard_metrics_client.py'}
if {r['target'] for r in x['files']}!=allowed:raise ValueError('scope mismatch')
root=Path('/Users/cnc-media/services')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
for r in x['files']:
 p=root/r['target']
 if p.is_symlink() or sha(p)!=r['before']:raise ValueError('concurrent runtime change: '+r['target'])
 b=base64.b64decode(r['data'],validate=True)
 if hashlib.sha256(b).hexdigest()!=r['after']:raise ValueError('transport mismatch')
if not x['deploy']:
 print(json.dumps({'preflight':'green','files':len(x['files'])}));sys.exit()
backup=root/'mbd-dashboard-metrics'/'backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup.mkdir(parents=True,exist_ok=False,mode=0o700)
for r in x['files']:
 p=root/r['target'];p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists():
  dest=backup/r['target'];dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
 t=p.with_name(p.name+'.metrics-candidate')
 with open(t,'wb',opener=lambda f,flags:os.open(f,flags,0o600)) as h:h.write(base64.b64decode(r['data']))
 os.replace(t,p)
for r in x['files']:
 if sha(root/r['target'])!=r['after']:raise ValueError('readback failed')
print(json.dumps({'installed':True,'backup':str(backup),'files':{r['target']:r['after'] for r in x['files']}}))
'''


def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate-dir',required=True);p.add_argument('--deploy',action='store_true');a=p.parse_args()
    files=[]
    for target,name,before in SOURCES:
        source=Path(a.candidate_dir)/name if before else Path(__file__).parent/name
        data=source.read_bytes()
        files.append(dict(target=target,before=before,after=hashlib.sha256(data).hexdigest(),data=base64.b64encode(data).decode()))
    command='/usr/bin/python3 -B -c '+shlex.quote(REMOTE)
    r=subprocess.run(['ssh','-i',KEY,'-o','BatchMode=yes','-o','ConnectTimeout=10',HOST,command],
        input=json.dumps({'files':files,'deploy':a.deploy}),capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError('runtime preflight/deployment failed: '+r.stderr[-700:])
    print(r.stdout.strip())


if __name__=='__main__':main()
