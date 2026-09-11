"""Publish private derived metrics ONLY after exact public HTML readback.

Existing daily runner invokes this, so no scheduler or auth change is needed.
The only remote write is the explicitly scoped private metrics cache.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path
from export_bot_metrics import export

HOST = 'cnc-media@192.168.7.238'
KEY = '/Users/sb.lee/.ssh/id_ed25519_mbd_server'
TARGET = '/Users/cnc-media/services/mbd-dashboard-metrics/data/metrics.json'
URL = 'https://been3p-prog.github.io/mbd-h2-kr-dashboard/'
RECEIVE = r'''
import hashlib,json,os,sys
from pathlib import Path
p=Path('/Users/cnc-media/services/mbd-dashboard-metrics/data/metrics.json')
b=sys.stdin.buffer.read(8000001)
if len(b)>8000000: raise ValueError('oversize')
d=json.loads(b)
if d.get('schema')!='mbd-bot-metrics-v1': raise ValueError('schema')
p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
t=p.with_suffix('.candidate.json')
with open(t,'wb',opener=lambda f,flags:os.open(f,flags,0o600)) as h:h.write(b)
os.replace(t,p)
print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'dashboard_sha256':d['dashboard_sha256']}))
'''


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--mbd',required=True);p.add_argument('--youtube',required=True)
    p.add_argument('--html',default='index.html');p.add_argument('--check-only',action='store_true')
    a=p.parse_args()
    html=Path(a.html).read_bytes()
    sha=hashlib.sha256(html).hexdigest()
    req=urllib.request.Request(URL+'?bot-metrics='+sha[:12],headers={'Cache-Control':'no-cache'})
    with urllib.request.urlopen(req,timeout=25) as r:public=r.read(8_000_001)
    if public!=html:raise RuntimeError('public HTML not the local candidate; no cache write')
    cutoff=dt.date.fromisoformat(re.search(rb'data-current-as-of="([0-9-]+)"',html)[1].decode())
    payload=export(a.mbd,a.youtube,a.html,cutoff)
    data=json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    if a.check_only:
        print(json.dumps({'verified':True,'dashboard_sha256':sha,'bytes':len(data)}));return
    # Constant command, no question/source text interpolated into a remote shell.
    import shlex
    command='/usr/bin/python3 -B -c '+shlex.quote(RECEIVE)
    result=subprocess.run(['ssh','-i',KEY,'-o','BatchMode=yes','-o','ConnectTimeout=10',HOST,command],
                          input=data,capture_output=True,timeout=40,check=True)
    receipt=json.loads(result.stdout)
    if receipt.get('sha256')!=hashlib.sha256(data).hexdigest() or receipt.get('dashboard_sha256')!=sha:
        raise RuntimeError('private cache exact readback mismatch')
    print(json.dumps({'verified':True,**receipt}))


if __name__=='__main__':main()
