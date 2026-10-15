"""Select existing actors in a read-only pod transaction; privately collect 15-minute JWTs.

Run locally with --ssh-target root@HOST [--ssh-key PATH]. Only metadata is printed.
When SSHPASS is present, sshpass -e supplies it without putting it in argv.
No password resets, membership/grant edits, startup, migrations or task writes.
"""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

PROTECTED_PRODUCT = 'qa-vcq-a044ea9ea181-p2'
PROTECTED_ORDER = 'ORD-261012005144E1B3'


def private_write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)


def choose(m, db):
    from sqlalchemy import select
    valid = [m.User.is_active.is_(True), m.User.activation_status == 'active', m.User.verified_status == 'verified']
    actors = {}
    for name, role in [('Admin', 'super_admin'), ('Operator', 'platform_operator')]:
        user = db.scalar(select(m.User).where(*valid, m.User.platform_role == role).order_by(m.User.created_at, m.User.id))
        if user:
            actors[name] = {'user': user, 'enterprise_id': ''}
    memberships = db.execute(select(m.Membership, m.User).join(m.User, m.User.id == m.Membership.user_id)
        .join(m.Enterprise, m.Enterprise.id == m.Membership.enterprise_id)
        .where(*valid, m.User.platform_role == '', m.Membership.status == 'active', m.Enterprise.verification_status == 'verified')
        .order_by(m.Membership.created_at, m.Membership.id)).all()
    for membership, user in memberships:
        if membership.role in {'super_admin', 'enterprise_admin'} and 'Provider' not in actors:
            actors['Provider'] = {'user': user, 'enterprise_id': membership.enterprise_id}
        if membership.role != 'member' or 'Member' in actors:
            continue
        subscriptions = db.scalars(select(m.SaaSSubscription).join(m.Product, m.Product.id == m.SaaSSubscription.product_id)
            .where(m.SaaSSubscription.enterprise_id == membership.enterprise_id,
                m.Product.name != PROTECTED_PRODUCT, m.Product.id != PROTECTED_PRODUCT,
                m.Product.product_type == 'saas').order_by(m.SaaSSubscription.created_at, m.SaaSSubscription.id)).all()
        for subscription in subscriptions:
            try:
                m.applications_access(db, user, subscription.id)
            except m.HTTPException:
                continue
            # Select a real non-owner entitlement without changing grants or subscription status.
            orders = db.scalars(select(m.Order).where(m.Order.buyer_enterprise_id == membership.enterprise_id,
                m.Order.product_id == subscription.product_id, m.Order.payment_status == 'paid')).all()
            if not orders or any(order.buyer_user_id == user.id or order.order_no == PROTECTED_ORDER for order in orders):
                continue
            enterprises = [member.enterprise_id for member, target in memberships if target.id == user.id]
            actors['Member'] = {'user': user, 'enterprise_id': membership.enterprise_id,
                'subscription_id': subscription.id, 'product_id': subscription.product_id,
                'other_enterprise_ids': sorted(set(enterprises) - {membership.enterprise_id}),
                'non_order_owner': True}
            break
    return actors


def export_pod():
    from sqlalchemy import text
    from app import main as m
    private = Path(tempfile.mkdtemp(prefix='qa-r3-fe-actors-', dir='/tmp'))
    records = []
    with m.SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        for role, item in choose(m, db).items():
            user = item.pop('user')
            issued = m.now()
            token = m.jwt.encode({'sub': user.id, 'email': user.email, 'iat': issued,
                'exp': issued + timedelta(minutes=15)}, m.JWT_SECRET, algorithm='HS256')
            path = private / (role + '.token')
            private_write(path, token)
            records.append({'actor': role, 'user_id': user.id, 'token_file': str(path), **item})
        db.rollback()
    return {'private_dir': str(private), 'actors': records, 'database_mode': 'READ ONLY', 'jwt_ttl_seconds': 900,
            'missing': [name for name in ('Admin', 'Operator', 'Provider', 'Member') if name not in {item['actor'] for item in records}]}


def ssh_command(args, environ=None):
    environ = os.environ if environ is None else environ
    password = bool(environ.get('SSHPASS'))
    ssh = (['sshpass', '-e'] if password else []) + ['ssh', '-o',
        'BatchMode=no' if password else 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR']
    if password:
        ssh += ['-o', 'PreferredAuthentications=password,keyboard-interactive', '-o', 'NumberOfPasswordPrompts=1']
    elif args.ssh_key:
        ssh += ['-o', 'IdentitiesOnly=yes', '-i', args.ssh_key]
    ssh += [args.ssh_target]
    return ssh


def collect(args):
    ssh = ssh_command(args)
    pod = ['kubectl', '-n', 'market', 'exec', '-i', 'deployment/market-api', '--']

    def remote(command, content=None):
        result = subprocess.run(ssh + [shlex.join(command)], input=content, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError('SSH/pod operation failed; private stderr suppressed')
        return result.stdout

    # Transport output contains no JWT: the pod writes 0600 files and returns paths only.
    data = json.loads(remote(pod + ['python', '-', '--pod-export'], Path(__file__).read_text()))
    pod_private = Path(data['private_dir'])
    original_paths = [item['token_file'] for item in data['actors']]
    local = Path(tempfile.mkdtemp(prefix='qa-r3-fe-actors-', dir='/tmp'))
    if pod_private.parent != Path('/tmp') or not pod_private.name.startswith('qa-r3-fe-actors-'):
        raise RuntimeError('Unexpected private directory')
    try:
        for item in data['actors']:
            path = item['token_file']
            if Path(path).parent != pod_private or Path(path).name != item['actor'] + '.token':
                raise RuntimeError('Unexpected token path')
            token = remote(pod + ['cat', path]).strip()
            if token.count('.') != 2 or len(token) > 16384:
                raise RuntimeError('Invalid token transport')
            target = local / Path(path).name
            private_write(target, token)
            item['token_file'] = str(target)
        data['private_dir'] = str(local)
        private_write(local / 'actors.json', json.dumps(data))
        print(json.dumps(data))
    finally:
        # Remove only exact files returned by this run, without prefix globbing.
        for original in original_paths:
            if Path(original).parent == pod_private and Path(original).name in {role + '.token' for role in ('Admin', 'Operator', 'Provider', 'Member')}:
                remote(pod + ['python', '-c', 'from pathlib import Path; Path(' + repr(original) + ').unlink(missing_ok=True)'])
        remote(pod + ['python', '-c', 'from pathlib import Path; Path(' + repr(str(pod_private)) + ').rmdir()'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pod-export', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--ssh-target')
    parser.add_argument('--ssh-key')
    args = parser.parse_args()
    try:
        if args.pod_export:
            print(json.dumps(export_pod()))
        elif args.ssh_target:
            collect(args)
        else:
            parser.error('--ssh-target is required')
    except Exception:
        raise SystemExit('Actor selection/collection failed; no private diagnostics emitted')
