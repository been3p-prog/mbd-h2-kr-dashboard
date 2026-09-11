// Read-only subprocess; no shell, no Slack calls, no configuration changes.
import { spawn } from 'node:child_process';
export function dashboardCandidate(text) {
  if (/취소\s*해|수정\s*해|이동\s*해|등록\s*해|삭제\s*해|만들어|기획해|방법|매뉴얼|정책|세금|정산|부킹 기준|부킹률/.test(text)) return false;
  return /성과|실적|매출|거래액|예측|지표/.test(text);
}
export async function dashboardAnswer(question) {
  return new Promise((resolve) => {
    const child = spawn('/usr/bin/python3', ['-B', '/Users/cnc-media/services/mbd-dashboard-metrics/dashboard_metrics_client.py'], { stdio: ['pipe','pipe','ignore'] });
    let output = '';
    const failure = '🙏 *확인 필요*\n• 대시보드 공통 지표 연결을 확인하지 못했습니다. 다른 집계로 대체하지 않습니다.';
    const timer = setTimeout(() => {child.kill(); resolve(failure);}, 18000);
    child.stdout.on('data', chunk => {output += chunk; if(output.length>40000) child.kill();});
    child.on('error', () => {clearTimeout(timer);resolve(failure);});
    child.on('close', code => {clearTimeout(timer);try {const x=JSON.parse(output).answer;resolve(code===0 && typeof x==='string' ? x : failure);}catch{resolve(failure);}});
    child.stdin.on('error',()=>{});
    child.stdin.end(JSON.stringify({domain:'ads',question}));
  });
}
