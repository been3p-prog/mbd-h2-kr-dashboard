"""Explicit opt-in E2E test: hardcoded been_jobs ONLY. No raw reply persistence.

Uses existing user-authorized Slack connection. Never changes channel membership,
credentials, bot settings, source data or messages in any other channel.
"""
import argparse
import datetime as dt
import json
import re
import time
from pathlib import Path
from dashboard_metrics_client import answer,KST

CHANNEL='C086M0WDKPC'
BOTS={'live':'U0B4QTJ68BT','youtube':'U0BQB1QNGA0','ads':'U0BEPKJKMTL'}
QUESTIONS=[
 ('live','month','9월 라이브 성과'),('youtube','month','9월 유튜브 성과'),('ads','month','9월 광고 매출'),
 ('live','week','지난주 라이브 성과'),('youtube','week','지난주 유튜브 성과'),('ads','week','지난주 광고 매출'),
 ('live','day','9월 10일 라이브 상세'),('youtube','day','9월 10일 유튜브 성과 상세'),('ads','day','9월 10일 광고 매출'),
 ('live','brand','9월 시몬스 라이브 성과'),('youtube','detail','8월 가장 잘된 롱폼 하나랑 숏폼 하나 알려줘'),('ads','detail','8월 일반광고 매출 상세'),
 ('youtube','schedule','9월 유튜브 편성'),
 ('youtube','verified_day','9월 8일 유튜브 조회수'),
 ('youtube','platform_format','9월 숏폼 조회수'),
]


def normalize_reply(text):
    # Exact transport aliases observed in replies, including public video titles.
    # Preserve every number, qualifier, link and punctuation character.
    for unicode,shortcode in [('⚠️',':warning:'),('⚠',':warning:'),('🔴',':red_circle:'),('🏠',':house:')]:
        text=text.replace(unicode,shortcode)
    return re.sub(r'\s+', ' ', text).strip()


def main():
    import requests
    p=argparse.ArgumentParser();p.add_argument('--send',action='store_true');p.add_argument('--packet',required=True)
    p.add_argument('--receipt',required=True);p.add_argument('--group',choices=['all','month','rest'],default='all')
    p.add_argument('--domain',choices=list(BOTS))
    p.add_argument('--allow-app-authored',action='store_true',help='YouTube-only transport probe; other bots intentionally ignore apps')
    a=p.parse_args()
    if not a.send:raise SystemExit('Explicit --send required; been_jobs only')
    if a.allow_app_authored and a.domain!='youtube':raise SystemExit('App-authored probes are YouTube-only')
    packet=json.loads(Path(a.packet).read_text())
    cfg=json.loads(Path('/Users/sb.lee/automations/slack-digest/config.json').read_text())
    h={'Authorization':'Bearer '+cfg['slack_user_token']}
    def api(method,payload,write=False):
        if write and (method!='chat.postMessage' or payload.get('channel')!=CHANNEL):raise ValueError('write scope')
        for attempt in range(3):
            r=requests.post('https://slack.com/api/'+method,headers=h,json=payload,timeout=20) if write else requests.get('https://slack.com/api/'+method,headers=h,params=payload,timeout=20)
            if r.status_code==429:time.sleep(min(30,int(r.headers.get('Retry-After','5'))));continue
            d=r.json()
            if not d.get('ok'):raise RuntimeError('Slack '+method+': '+str(d.get('error')))
            return d
        raise RuntimeError('Slack rate limit')
    info=api('conversations.info',{'channel':CHANNEL})['channel']
    if info.get('id')!=CHANNEL or info.get('name')!='been_jobs' or info.get('is_archived'):raise ValueError('channel identity')
    pending=[]
    for domain,key,q in QUESTIONS:
        if a.domain and domain!=a.domain:continue
        if a.group=='month' and key!='month':continue
        if a.group=='rest' and key=='month':continue
        expected=answer(packet,q,domain,dt.datetime.now(KST).date())
        if not expected or expected.startswith('🙏'):raise RuntimeError('invalid local test expectation '+key)
        sent=api('chat.postMessage',{'channel':CHANNEL,'text':f'<@{BOTS[domain]}> {q}','unfurl_links':False,'unfurl_media':False},True)
        if sent.get('message',{}).get('bot_id') and not a.allow_app_authored:
            print(json.dumps({'channel':CHANNEL,'root_ts':sent['ts'],'reason':'app-authored; stopped without changing bot guard'}),flush=True)
            raise RuntimeError('Test message was app-authored; preserve bot-loop guard and stop')
        pending.append(dict(domain=domain,key=key,ts=sent['ts'],expected=expected))
        print(json.dumps({'sent':True,'domain':domain,'case':key,'channel':CHANNEL,'ts':sent['ts']}),flush=True)
        time.sleep(1.1)
    results=[]
    deadline=time.monotonic()+100
    while pending and time.monotonic()<deadline:
        for item in list(pending):
            reply=api('conversations.replies',{'channel':CHANNEL,'ts':item['ts'],'limit':20})
            actual=next((m for m in reply.get('messages',[]) if m.get('user')==BOTS[item['domain']] and m.get('ts')!=item['ts']),None)
            if actual is None:continue
            ok=normalize_reply(actual.get('text',''))==normalize_reply(item['expected'])
            result={'domain':item['domain'],'case':item['key'],'ok':ok,'channel':CHANNEL,'root_ts':item['ts'],'reply_ts':actual['ts'],
                    'permalink':'https://ohou-se.slack.com/archives/'+CHANNEL+'/p'+actual['ts'].replace('.','')}
            results.append(result);pending.remove(item)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if not ok:print('MISMATCH_SAFE_PREVIEW '+actual.get('text','')[:120],flush=True)
        if pending:time.sleep(5)
    for item in pending:results.append({'domain':item['domain'],'case':item['key'],'ok':False,'root_ts':item['ts'],'reason':'no bot reply before deadline'})
    receipt={'channel':CHANNEL,'dashboard_sha256':packet['dashboard_sha256'],'results':results,'passed':sum(r['ok'] for r in results),'total':len(results)}
    Path(a.receipt).write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':receipt['passed'],'total':receipt['total']}),flush=True)
    if receipt['passed']!=receipt['total']:raise SystemExit(1)


if __name__=='__main__':main()
