"""Guarded release of the existing YouTube weekly lane; no auth/scheduler edits.

Requires explicit deployment and exact installed hash. The existing bot process
must subsequently be restarted to import the new module.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

REMOTE = r'''
import base64,datetime,hashlib,json,os,shutil,sys
from pathlib import Path
x=json.load(sys.stdin)
p=Path('/Users/cnc-media/services/youtube-copilot/youtube_copilot/weekly_rules.py')
b=base64.b64decode(x['data'],validate=True)
compile(b,str(p),'exec')
def sha(file):return hashlib.sha256(file.read_bytes()).hexdigest()
if p.is_symlink() or sha(p)!=x['before']:raise ValueError('weekly baseline changed')
if hashlib.sha256(b).hexdigest()!=x['after']:raise ValueError('candidate hash mismatch')
if not x['deploy']:
 print(json.dumps({'preflight':True,'before':sha(p),'after':x['after']}));sys.exit()
backup=p.parent.parent/'backups'/('weekly-answer-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True,mode=0o700,exist_ok=False)
shutil.copy2(p,backup/p.name)
t=p.with_suffix('.candidate')
with open(t,'wb',opener=lambda f,flags:os.open(f,flags,0o600)) as h:h.write(b)
os.chmod(t,p.stat().st_mode & 0o777)
os.replace(t,p)
if sha(p)!=x['after']:raise ValueError('weekly readback failed')
print(json.dumps({'installed':True,'target':str(p),'sha256':sha(p),'backup':str(backup)}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-sha',required=True)
    parser.add_argument('--deploy',action='store_true')
    args=parser.parse_args()
    source=Path(__file__).with_name('youtube_weekly_rules.py').read_bytes()
    compile(source,'youtube_weekly_rules.py','exec')
    payload=dict(before=args.expected_sha,after=hashlib.sha256(source).hexdigest(),
                 data=base64.b64encode(source).decode(),deploy=args.deploy)
    result=subprocess.run(['ssh','-i','/Users/sb.lee/.ssh/id_ed25519_mbd_server',
        '-o','IdentitiesOnly=yes','-o','BatchMode=yes','cnc-media@192.168.7.238',
        '/usr/bin/python3 -B -c '+shlex.quote(REMOTE)],
        input=json.dumps(payload),text=True,capture_output=True,timeout=40)
    if result.returncode:raise RuntimeError('weekly guarded update failed')
    print(result.stdout.strip())


if __name__=='__main__':main()
