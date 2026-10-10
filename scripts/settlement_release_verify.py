"""Read-only deployed smoke checks and optional task acceptance synchronization."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://192.168.10.10:30080')
    parser.add_argument('--update-tasks', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    token = ''

    def call(path, method='GET', body=None, raw=False):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        req = Request(args.base + '/api' + path, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
        with urlopen(req, timeout=45) as response:
            payload = response.read()
            assert response.status == 200, response.status
            return payload if raw else json.loads(payload)

    token = call('/auth/login', 'POST', {'email': 'admin@market.local',
                                       'password': os.environ['MARKET_ADMIN_PASSWORD']})['token']
    for route in ('/dashboard', '/orders', '/audit-logs?start=&end=&page=1&page_size=50',
                  '/audit-logs/categories', '/settlement-measurements'):
        call(route)
        print('PASS', route)
    events = call('/audit-logs?page_size=1')['items']
    if events:
        call('/audit-logs/' + events[0]['id'] + '/timeline')
        print('PASS audit timeline')
    export = call('/audit-logs/export?category=settlement_all', raw=True).decode('utf-8-sig')
    assert 'order_no' in export and 'before' in export
    print('PASS filtered audit CSV')
    batches = call('/settlement-batches')['items']
    if batches:
        call('/settlement-batches/' + batches[0]['id'] + '/ledger-reports')
        print('PASS ledger reports')
    completed = {
        'BE-022': '人工计量幂等与权限、支付/退款/交付/下载事件及共享API累计快照通过；计量不替代支付月份清算、不重复按订单累计共享用量。',
        'BE-028': '清算事件关联订单/批次/规则及请求号，结构化前后值与按订单隔离时间线验证通过。',
        'BE-029': '分类、时间、组合查询、分页、CSV导出、公式防护、导出上限及越权拒绝验证通过。',
        'FE-013': '审计分类、时间筛选、CSV导出、前后值、时间线分页和业务跳转已实现；真实浏览器桌面及移动端验证通过。',
        'QA-007': '隔离库审计自动测试通过；真实PG两万条记录查询基线及真实浏览器检索/详情/导出通过，不代表生产容量承诺。',
        'QA-010': '四阶段审核、角色及企业隔离、通知和事务回滚通过；旧直接publish入口已封堵。',
        'ARC-006': '当前可实施计量、清算保护、审计、账单导入设计及验证证据归档；真实资金联调限制分别保留于BE-024、QA-006。',
    }
    pending = {
        'BE-024': (70, '已完成：独立JSON账单导入、幂等、四类账簿逐订单比较、漏单/多单/重复/退款负向差异、处理依据和报告下载及前端。未完成：真实支付/银行/分账渠道适配与验收；缺账单/API、商户账户、回执、授权及手续费/到账时差映射。暂用独立模拟或人工导入验证，real_channel_verified=false，不触发真实付款。'),
        'QA-006': (90, '已完成：内部清算、幂等并发、调整提案保护、退款负向、权限回归及四类独立模拟账单测试。未完成：真实支付、银行、实际分账回执的资金端到端联调；待BE-024外部数据源与测试账户到位。模拟通过不等同真实支付验收。'),
    }
    if args.update_tasks:
        for code, acceptance in completed.items():
            call('/development/tasks/' + code, 'PATCH', {'status': 'done', 'progress': 100,
                  'acceptance': acceptance, 'note': '完成可实施开发、部署及验收；详细证据见20261009收尾文档。'})
        for code, (progress, acceptance) in pending.items():
            call('/development/tasks/' + code, 'PATCH', {'status': 'blocked', 'progress': progress,
                  'acceptance': acceptance, 'note': acceptance})
    tasks = call('/development/tasks')
    print(json.dumps({'total': tasks['total'], 'counts': tasks['counts']}, ensure_ascii=False))
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        snapshot = {**tasks, 'items': [t for t in tasks['items'] if t['code'] in completed or t['code'] in pending]}
        (args.output / '任务收尾状态-20261009.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=['code', 'title', 'status', 'progress', 'acceptance'], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(snapshot['items'])
        (args.output / '任务收尾状态-20261009.csv').write_text(out.getvalue(), encoding='utf-8-sig')


if __name__ == '__main__':
    main()
