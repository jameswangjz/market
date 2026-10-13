"""Update the two defect tasks only with matching live acceptance evidence."""
import argparse
import json
import os
from pathlib import Path


def validate(proof):
    if (proof.get('status') != 'passed' or
            proof.get('scope') != 'OAuth cache 401 recovery and isolated PostgreSQL startup fixes' or
            proof.get('lua_harness') != 'passed' or
            proof.get('saas', {}).get('expired_401_cache_auto_evicted') is not True or
            proof.get('saas', {}).get('expired_signed_warm_token_next_attempt_recovered') is not True or
            proof.get('saas', {}).get('individually_revoked_warm_token_next_attempt_recovered') is not True or
            set(proof.get('native_gateway', {})) != {'api', 'model'} or
            any(item.get('revoked_token_next_call_after_auto_eviction') != 200
                or item.get('atomic_new_token_preserved') is not True
                or item.get('atomic_exact_token_deleted') is not True
                for item in proof['native_gateway'].values()) or
            proof.get('startup', {}).get('valid_buyer_foreign_key') is not True or
            proof.get('startup', {}).get('repeat_core_rows_unchanged') is not True or
            proof.get('startup', {}).get('platform_memberships') != 0 or
            proof.get('cleanup_verified') is not True):
        raise ValueError('Live cache recovery and isolated PostgreSQL startup proof required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('start', 'verified'), required=True)
    parser.add_argument('--proof')
    args = parser.parse_args()
    proof = json.loads(Path(args.proof).read_text()) if args.proof else {}
    if args.phase == 'verified':
        validate(proof)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from sqlalchemy import select
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not actor:
            raise ValueError('Audit actor unavailable')
        for code in ('FLT-BE-001', 'FLT-BE-002'):
            task = db.scalar(select(m.DevelopmentTask).where(m.DevelopmentTask.code == code))
            if not task:
                raise ValueError('Task missing: ' + code)
            if args.phase == 'start' and task.status in {'done', 'review'}:
                raise ValueError('Refusing to restart completed task: ' + code)
            note = ('开始修复，补充并发缓存保护及真实PG初始化回归。' if args.phase == 'start' else
                    proof['runtime_tag'] + '; 实际SaaS/原生网关401后下一次安全调用恢复、并发新Token保护及首次/重复PG初始化验证通过；不重放当前业务请求，平台账号未加入企业。')
            m.update_development_task(code, m.DevelopmentTaskUpdate(
                status='in_progress' if args.phase == 'start' else 'done',
                progress=max(task.progress, 20) if args.phase == 'start' else 100,
                note=note), actor, db)
        snapshot = m.development_tasks(actor, db)
    print(json.dumps({'counts': snapshot['counts'], 'tasks': ['FLT-BE-001', 'FLT-BE-002']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
