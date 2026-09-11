"""Subsequent release: replace only the shared read-only engine, not bot services.

Explicit --deploy, exact previous hash, source syntax check, backup (engine/cache),
atomic install and checksum readback. Existing adapters spawn this file per query;
no bot restart, auth, scheduler or membership change is needed.
"""
import argparse
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

REMOTE=r'''
import base64,datetime,hashlib,json,os,shutil,sys
from pathlib import Path
x=json.load(sys.stdin)
p=Path('/Users/cnc-media/services/mbd-dashboard-metrics/dashboard_metrics_client.py')
b=base64.b64decode(x['data'],validate=True)
compile(b,str(p),'exec')
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
if p.is_symlink() or sha(p)!=x['before']:raise ValueError('shared client baseline changed')
if hashlib.sha256(b).hexdigest()!=x['after']:raise ValueError('candidate hash mismatch')
if not x['deploy']:
 print(json.dumps({'preflight':True,'before':sha(p),'after':x['after']}));sys.exit()
backup=p.parent/'backups'/('api-connect-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True,mode=0o700,exist_ok=False)
shutil.copy2(p,backup/p.name)
cache=p.parent/'data/metrics.json'
if cache.exists():shutil.copy2(cache,backup/'metrics.json')
t=p.with_suffix('.api-candidate')
with open(t,'wb',opener=lambda f,flags:os.open(f,flags,0o600)) as h:h.write(b)
os.replace(t,p)
if sha(p)!=x['after']:raise ValueError('shared client readback failed')
print(json.dumps({'installed':True,'target':str(p),'sha256':sha(p),'backup':str(backup)}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-sha',required=True)
    parser.add_argument('--deploy',action='store_true')
    args=parser.parse_args()
    source=Path(__file__).with_name('dashboard_metrics_client.py').read_bytes()
    compile(source,'dashboard_metrics_client.py','exec')
    payload=dict(before=args.expected_sha,after=hashlib.sha256(source).hexdigest(),
        data=base64.b64encode(source).decode(),deploy=args.deploy)
    result=subprocess.run(['ssh','-i','/Users/sb.lee/.ssh/id_ed25519_mbd_server','-o','BatchMode=yes',
        'cnc-media@192.168.7.238','/usr/bin/python3 -B -c '+shlex.quote(REMOTE)],
        input=json.dumps(payload),text=True,capture_output=True,timeout=40)
    if result.returncode:raise RuntimeError('shared client guarded update failed')
    print(result.stdout.strip())


if __name__=='__main__':main()
