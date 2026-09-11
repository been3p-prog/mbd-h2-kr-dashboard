"""Small read-only bridge, copied into each Python bot without auth changes."""
import json
import re
import subprocess
import sys

CLIENT = '/Users/cnc-media/services/mbd-dashboard-metrics/dashboard_metrics_client.py'


def dashboard_answer(question, domain):
    if not re.search(r'성과|실적|매출|gmv|GMV|거래액|예측|부킹|편성|지표|조회수|구독자|잘된|영상별|콘텐츠별|상세', question):
        return None
    try:
        result = subprocess.run([sys.executable, '-B', CLIENT],
            input=json.dumps({'domain':domain,'question':question}, ensure_ascii=False),
            capture_output=True, text=True, timeout=18, check=True)
        value = json.loads(result.stdout)['answer']
        if value is not None and not isinstance(value,str):
            raise ValueError('invalid answer')
        return value
    except Exception:
        return '🙏 *확인 필요*\n• 공통 지표 연결을 확인하지 못했습니다. 다른 집계나 추정 수치로 대체하지 않습니다.'
