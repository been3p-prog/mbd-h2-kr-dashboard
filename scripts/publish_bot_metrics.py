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
from html.parser import HTMLParser
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


class _CutoffParser(HTMLParser):
    """Collect clocks only inside their owning mvk surface, not adjacent months."""

    def __init__(self):
        super().__init__()
        self.manifests = []
        self.manifest = None
        self.surfaces = []
        self.surface = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        is_manifest = any(k == 'id' and v == 'mbd-public-guard' for k, v in attrs)
        is_surface = any(k == 'class' and 'mvk' in (v or '').split() for k, v in attrs)
        if is_manifest or is_surface or (self.surface is not None and 'data-current-as-of' in values):
            if len(values) != len(attrs):
                raise ValueError('ambiguous cutoff attributes')
        if is_manifest:
            if tag != 'script' or values.get('type') != 'application/json':
                raise ValueError('malformed dashboard manifest')
            self.manifest = []
        if tag == 'div':
            self.depth += 1
        if is_surface:
            month = values.get('data-m') or ''
            if tag != 'div' or self.surface is not None or not re.fullmatch(r'[1-9]|1[0-2]', month):
                raise ValueError('malformed month surface')
            self.surface = {'month': int(month), 'depth': self.depth, 'cutoffs': []}
        if self.surface is not None and 'data-current-as-of' in values:
            self.surface['cutoffs'].append(values['data-current-as-of'])

    def handle_endtag(self, tag):
        if tag == 'script' and self.manifest is not None:
            self.manifests.append(''.join(self.manifest))
            self.manifest = None
        if tag == 'div':
            if self.surface is not None and self.depth == self.surface['depth']:
                self.surfaces.append(self.surface)
                self.surface = None
            self.depth -= 1

    def handle_data(self, data):
        if self.manifest is not None:
            self.manifest.append(data)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('ambiguous dashboard manifest key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('malformed dashboard manifest constant')


def dashboard_cutoff(html):
    parser = _CutoffParser()
    parser.feed(html.decode('utf-8'))
    parser.close()
    if parser.manifest is not None or len(parser.manifests) != 1:
        raise ValueError('missing or ambiguous dashboard manifest')
    manifest = json.loads(parser.manifests[0], object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
    month = manifest.get('default_month') if isinstance(manifest, dict) else None
    if type(month) is not int or not 1 <= month <= 12:
        raise ValueError('malformed default month')
    surfaces = [s for s in parser.surfaces if s['month'] == month]
    if parser.surface is not None or len(surfaces) != 1:
        raise ValueError('missing or ambiguous default month surface')
    cutoffs = surfaces[0]['cutoffs']
    if len(cutoffs) != 1 or not isinstance(cutoffs[0], str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', cutoffs[0]):
        raise ValueError('missing, ambiguous or malformed default month cutoff')
    cutoff = dt.date.fromisoformat(cutoffs[0])
    if cutoff.month != month:
        raise ValueError('default month differs from cutoff')
    return cutoff


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
    cutoff=dashboard_cutoff(html)
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
