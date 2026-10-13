"""Record the scoped R2 batch; preserve unfinished tasks and evidence boundaries."""
import argparse
import json
import os
from pathlib import Path


R2_CODES = (
    'TRD-R2-ARC-001', 'TRD-R2-ARC-002',
    *(f'TRD-R2-BE-{number:03d}' for number in range(1, 9)),
    *(f'TRD-R2-FE-{number:03d}' for number in range(1, 8)),
    'TRD-R2-OPS-001', 'TRD-R2-QA-001', 'TRD-R2-QA-002',
)


def batch2_updates(evidence):
    """Validate reported evidence; coverage is a list of full R2 task codes.

    This gate does not independently verify deployment or external services.
    None status/progress means retain the task's persisted values.
    """
    if not isinstance(evidence, dict) or evidence.get('status') != 'passed':
        raise ValueError('Evidence status must be passed')
    backend = evidence.get('backend')
    if not isinstance(backend, dict):
        raise ValueError('Evidence backend must contain tests, failures and errors')
    for key in ('tests', 'failures', 'errors'):
        if type(backend.get(key)) is not int or backend[key] < 0:
            raise ValueError('Evidence backend.' + key + ' must be a nonnegative integer')
    if backend['tests'] == 0 or backend['failures'] != 0 or backend['errors'] != 0:
        raise ValueError('Backend evidence requires executed tests with zero failures/errors')
    postgres = evidence.get('postgres')
    if (not isinstance(postgres, dict) or postgres.get('status') != 'passed'
            or postgres.get('cleanup_verified') is not True):
        raise ValueError('Postgres evidence requires passed status and cleanup_verified=true')
    frontend = evidence.get('frontend')
    if not isinstance(frontend, dict) or frontend.get('status') != 'passed':
        raise ValueError('Frontend evidence status must be passed')
    deployment = evidence.get('deployment')
    tag = deployment.get('tag') if isinstance(deployment, dict) else None
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError('Deployment evidence requires a nonempty tag')
    coverage = evidence.get('coverage', [])
    if (not isinstance(coverage, list)
            or any(not isinstance(code, str) or code not in R2_CODES for code in coverage)):
        raise ValueError('Evidence coverage must list full R2 task codes')
    covered = set(coverage)
    prefix = (f'gpt6.1-sol low；batch2证据记录：后端{backend["tests"]}项测试，'
              f'失败0/错误0；PG passed且清理已核验；前端passed；部署标签{tag.strip()}。'
              '模拟测试不代表真实外部支付、API或SaaS联调。')
    updates = {}
    for code in R2_CODES:
        if code in covered:
            updates[code] = ('review', 100, prefix + '证据coverage列明本任务，提交代码范围待验收。')
        else:
            updates[code] = (None, None, prefix + '证据coverage未列明本任务，保留原状态和进度。')
    updates['TRD-R2-ARC-001'] = ('review', 100, prefix + '需求、历史冲突和迁移设计已冻结，提交审查。')
    updates['TRD-R2-BE-008'] = ('in_progress', 85, prefix +
        '提供方清算仅只读查询；调整、详情和审核工作流尚未完成。')
    updates['TRD-R2-QA-002'] = ('in_progress', 80, prefix +
        '浏览器全流程覆盖有限；全部交付形态的注册、审核、接入、发布、购买、支付和同步仍待验证。')
    updates['TRD-R2-ARC-002'] = ('in_progress', 85, prefix +
        '证据归档仍有缺口：提供方清算调整/详情/审核、全部交付形态浏览器全流程及真实外部联调。')
    return updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--verified', action='store_true')
    parser.add_argument('--batch2', action='store_true', help='Record all 20 R2 tasks using verified evidence')
    parser.add_argument('--evidence', type=Path, help='Batch2 JSON: status, backend, postgres, frontend, deployment and coverage')
    args = parser.parse_args()
    batch2 = None
    if args.batch2:
        if args.evidence is None:
            parser.error('--evidence is required with --batch2')
        try:
            batch2 = batch2_updates(json.loads(args.evidence.read_text(encoding='utf-8')))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    updates = {
        'TRD-R2-ARC-001': ('review', 100, '本轮需求、历史冲突矩阵、详细设计和20项任务完成；新增API统一最新调用规范，无旧调用方兼容期。'),
        'TRD-R2-FE-001': ('in_progress', 60, 'gpt-6-luna medium完成租户开关删除、真实角色标签和配额合并；当前模型审查，浏览器验证待完成。'),
        'TRD-R2-FE-002': ('in_progress', 60, 'gpt-6-luna medium完成支付响应即时应用、详情支付单更新及旧列表请求隔离；浏览器真实确认支付验证待完成。'),
        'TRD-R2-FE-003': ('in_progress', 20, '已移除登记兼容性控件，并同步提供方/购买方规范和前端下载源；类型联动、自动路由及其它登记规则待后端配套。'),
    }
    if args.verified:
        updates['TRD-R2-FE-001'] = ('in_progress', 80, '当前模型验证185项前端测试及生产构建通过；角色、租户按钮、配额标签代码已验证，待实际浏览器验收。')
        updates['TRD-R2-FE-002'] = ('in_progress', 80, '当前模型验证185项前端测试及生产构建通过；支付单及订单即时更新、异步隔离通过，待浏览器模拟支付复测。')
    if batch2 is not None:
        updates = batch2
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Task actor unavailable')
        tasks = {}
        for code in updates:
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task:
                raise ValueError('Task missing: ' + code)
            tasks[code] = task
        updated = []
        for code, (status, progress, note) in updates.items():
            task = tasks[code]
            if task.status == 'done':
                continue
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status=task.status if status is None else status,
                progress=task.progress if progress is None else progress, note=note), actor, db)
            updated.append(code)
        result = m.development_tasks(actor, db)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'counts': result['counts'], 'updated': updated}, ensure_ascii=False))


if __name__ == '__main__':
    main()
