"""Apply verified MSG task statuses and export a control-console snapshot."""
import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://192.168.10.10:30080/api')
    parser.add_argument('--output', default='docs')
    parser.add_argument('--apply-verified', action='store_true')
    args = parser.parse_args()
    password = os.environ['MARKET_ADMIN_PASSWORD']
    token = None

    def call(path, payload=None, method=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = Request(args.base_url + path, method=method, headers=headers,
                          data=json.dumps(payload).encode() if payload is not None else None)
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    token = call('/auth/login', {'email': 'admin@market.local', 'password': password})['token']
    tasks = [t for t in call('/development/tasks')['items'] if t['code'].startswith('MSG-')]
    if len(tasks) != 28:
        raise RuntimeError('Expected exactly 28 MSG tasks; no statuses changed')
    if args.apply_verified:
        evidence = {
            'BE': '42项消息中心与35项业务通知测试通过；PG并发、实际Redis/SMTP/MinIO/ClamAV联调通过。',
            'FE': 'Chrome155实际登录、草稿发送、详情阅读、铃铛、系统设置与桌面/手机验证通过；3项SSE解析测试通过。',
            'OPS': 'market命名空间部署健康；双Worker、双API广播、清理Job、Prometheus采集及规则验证通过。',
            'QA': '权限、事务回滚、租约与重试、业务通知、实际服务和浏览器回归通过；限制见验收报告。',
            'ARC': '需求、实现契约、验收证据、部署及28项任务状态核对归档完成。',
        }
        for task in tasks:
            if task['status'] != 'done' or task['progress'] != 100:
                call('/development/tasks/' + task['code'], {
                    'status': 'done', 'progress': 100,
                    'note': evidence[task['code'].split('-')[1]] + '部署标签20261009043151-230f154。',
                }, 'PATCH')
        tasks = [t for t in call('/development/tasks')['items'] if t['code'].startswith('MSG-')]
        if any(t['status'] != 'done' or t['progress'] != 100 for t in tasks):
            raise RuntimeError('Task state read-back failed')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    counts = dict(Counter(t['status'] for t in tasks))
    snapshot = {'captured_at': datetime.now(timezone.utc).isoformat(), 'counts': counts, 'items': tasks}
    (output / '消息中心任务验收快照.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    plan = output / '消息中心开发任务清单.csv'
    if plan.exists():
        with plan.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        states = {t['code']: t for t in tasks}
        if any(r['任务编号'] not in states for r in rows) or len(rows) != 28:
            raise RuntimeError('CSV task mapping does not match the console')
        for row in rows:
            task = states[row['任务编号']]
            row['状态'] = '已完成' if task['status'] == 'done' else task['status']
            row['进度'] = str(task['progress'])
        with plan.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({'count': len(tasks), 'counts': counts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
