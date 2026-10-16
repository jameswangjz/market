"""Explicit, durable financial source transitions. Hooks own their transactions."""
import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import BigInteger, String, Text, select, text
from sqlalchemy.orm import mapped_column

from .subscription_terms import UTCDateTime


CAS = """
local actual = redis.call('HGETALL', KEYS[1])
local expected = cjson.decode(ARGV[1])
if #actual ~= 2 * tonumber(ARGV[3]) then return 0 end
for i=1,#actual,2 do
  if expected[actual[i]] ~= actual[i+1] then return 0 end
end
redis.call('DEL', KEYS[1])
local target = cjson.decode(ARGV[2])
for k,v in pairs(target) do redis.call('HSET', KEYS[1], k, v) end
return 1
"""


def install(ns, hooks, run_id, stream, epoch, microseconds):
    class SourceGeneration(ns['Base']):
        __tablename__ = 'api_charge_source_generations'
        id = mapped_column(String(40), primary_key=True)
        stream = mapped_column(String(128), nullable=False, unique=True)
        status = mapped_column(String(16), nullable=False, index=True)
        process_run_id = mapped_column(String(40), nullable=False)
        cutover = mapped_column(UTCDateTime(), nullable=False)
        sealed_hash = mapped_column(String(64), nullable=True)
        old_stream = mapped_column(String(128), nullable=True)
        old_cursor = mapped_column(String(48), nullable=True)

        marker_snapshot = mapped_column(Text, nullable=False)
        audit_log = mapped_column(Text, nullable=False, default='[]')

    class Gap(ns['Base']):
        __tablename__ = 'api_charge_source_gaps'
        id = mapped_column(String(40), primary_key=True)
        source_id = mapped_column(String(40), nullable=False, unique=True)
        actor = mapped_column(String(256), nullable=False)
        reason = mapped_column(Text, nullable=False)
        created_at = mapped_column(UTCDateTime(), nullable=False)
        old_stream = mapped_column(String(128), nullable=True)
        old_cursor = mapped_column(String(48), nullable=True)
        old_source_id = mapped_column(String(64), nullable=True)
        status = mapped_column(String(16), nullable=False, default='unresolved')
        missing_count = mapped_column(BigInteger, nullable=True)

    Cursor = hooks['Cursor']

    def lock(db):
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(723401830031)'))

    def latest(db):
        return db.scalar(select(SourceGeneration).order_by(
            SourceGeneration.cutover.desc(), SourceGeneration.id.desc()).limit(1))

    def marker(row, status='ready', physical=None):
        return dict(run_id=row.id, stream=row.stream, status=status,
                    process_run_id=physical or row.process_run_id,
                    not_before_us=str(microseconds(row.cutover)))

    def cas(client, before, after):
        if client.hgetall(epoch) == after:
            return
        if client.eval(CAS, 1, epoch, json.dumps(before), json.dumps(after), len(before)) != 1:
            raise ValueError('Financial source marker changed during transition')

    def source_context(db, client, allow_paused=False):
        row = latest(db)
        if row is None:
            return None
        expected = marker(row, 'paused' if allow_paused else 'ready')
        if (row.status != 'active' or run_id(client) != row.process_run_id
                or client.hgetall(epoch) != expected):
            raise ValueError('Managed financial source unavailable; explicit continuity review required')
        cursor = db.get(Cursor, row.stream)
        if cursor is None or cursor.redis_run_id != row.id or cursor.activated_at != row.cutover:
            raise ValueError('Managed source cursor registration mismatch')
        return row.stream, cursor

    def audit(actor, reason):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise ValueError('An audit actor is required')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('An audit reason is required')

    def info(row):
        # Unknown missing usage is NULL, never an inferred zero.
        return dict(source_id=row.id, stream=row.stream, status=row.status,
                    process_run_id=row.process_run_id, not_before_us=str(microseconds(row.cutover)),
                    gap_status='unresolved', missing_count=None, audits=json.loads(row.audit_log))

    def open_epoch(db, client, actor, reason):
        audit(actor, reason)
        lock(db)
        previous = latest(db)
        physical = run_id(client)
        if previous is not None and previous.status == 'pending' and previous.process_run_id == physical:
            row = previous
        else:
            before = client.hgetall(epoch)
            old_stream = previous.stream if previous else stream
            old = db.get(Cursor, old_stream)
            cutover = ns['now']()
            if previous is not None and cutover <= previous.cutover:
                cutover = previous.cutover + timedelta(microseconds=1)
            identity = hashlib.sha1(uuid.uuid4().bytes).hexdigest()
            row = SourceGeneration(id=identity, stream=stream + ':' + identity,
                status='pending', process_run_id=physical, cutover=cutover,
                old_stream=old_stream, old_cursor=old.last_id if old else None,
                audit_log=json.dumps([dict(action='open', actor=actor, reason=reason)]),
                marker_snapshot=json.dumps(before, sort_keys=True))
            if previous:
                if previous.status == 'pending':
                    # Only explicit admin open may abandon a pending source after process loss.
                    previous.status = 'abandoned'
                    previous.audit_log = json.dumps(json.loads(previous.audit_log) + [
                        dict(action='abandon', actor=actor, reason=reason)])
                else:
                    previous.status = 'paused'
            db.add(row)
            db.add(Gap(id=uuid.uuid4().hex, source_id=identity, actor=actor, reason=reason,
                created_at=cutover, old_stream=old_stream, old_cursor=row.old_cursor,
                old_source_id=old.redis_run_id if old else None,
                status='unresolved', missing_count=None))
            db.add(Cursor(stream=row.stream, last_id='0-0', redis_run_id=identity, activated_at=cutover))
            db.commit()
        cas(client, json.loads(row.marker_snapshot), marker(row))
        row.status = 'active'
        db.commit()
        return info(row)

    def seal_hash(client, row):
        # Minute limits are ephemeral; daily quotas and their absolute expiry are sealed below.
        records = []
        if client.pttl(epoch) != -1 or client.pttl(row.stream) not in (-1, -2):
            raise ValueError('Financial stream and marker must not expire')
        for pattern in ('market:apisix:quota:subscription:month:*', 'market:apisix:charges:coverage:*'):
            for key in sorted(client.scan_iter(match=pattern)):
                if client.pttl(key) != -1:
                    raise ValueError('Financial evidence must not expire')
                kind = client.type(key)
                value = client.get(key) if kind == 'string' else client.hgetall(key) if kind == 'hash' else None
                if value is None:
                    raise ValueError('Invalid financial evidence type')
                records.append((key, kind, value))
        for key in sorted(client.scan_iter(match='market:apisix:quota:subscription:day:*')):
            value = client.get(key)
            expiry = client.execute_command('PEXPIRETIME', key)
            if value is None or type(expiry) is not int or expiry < -1:
                raise ValueError('Daily quota expired or disappeared while sealing')
            records.append((key, 'string', value, expiry))
        frozen = client.hgetall(epoch)
        frozen.pop('process_run_id', None)
        frozen['status'] = 'paused'
        records.extend([(row.stream, client.xrange(row.stream)), ('marker', frozen)])
        return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def seal_epoch(db, client, actor, reason):
        audit(actor, reason)
        lock(db)
        row = latest(db)
        if row is None:
            raise ValueError('No managed source to seal')
        if row.status == 'paused' and row.sealed_hash:
            if client.hgetall(epoch) != marker(row, 'paused') or seal_hash(client, row) != row.sealed_hash:
                raise ValueError('Sealed source evidence changed')
            return info(row)
        if row.status != 'active' or run_id(client) != row.process_run_id:
            raise ValueError('Only a registered running source may be sealed')
        cas(client, marker(row), marker(row, 'paused'))
        # Pausing is atomic with gateway debits; drain every accepted event before sealing.
        while True:
            result = hooks['ingest'](db, client, limit=2000, _sealed=True)
            if result['read'] < 2000:
                break
        sealed_hash = seal_hash(client, row)
        row.status = 'paused'
        row.sealed_hash = sealed_hash
        row.audit_log = json.dumps(json.loads(row.audit_log) + [dict(action='seal', actor=actor, reason=reason)])
        db.commit()
        return info(row)

    def resume_epoch(db, client, actor, reason):
        audit(actor, reason)
        lock(db)
        row = latest(db)
        if row is None or row.status != 'paused' or not row.sealed_hash:
            raise ValueError('Unplanned restart requires an explicit gap epoch')
        before = client.hgetall(epoch)
        allowed = [marker(row, 'paused')]
        if row.marker_snapshot:
            allowed.append(json.loads(row.marker_snapshot))
        if run_id(client) == row.process_run_id:
            allowed.append(marker(row))
        if before not in allowed or seal_hash(client, row) != row.sealed_hash:
            raise ValueError('Sealed financial evidence changed; explicit gap epoch required')
        physical = run_id(client)
        # Persist the new physical binding before Redis can advertise readiness.
        row.marker_snapshot = json.dumps(before, sort_keys=True)
        row.process_run_id = physical
        db.commit()
        cas(client, before, marker(row))
        row.status = 'active'
        row.sealed_hash = None
        row.audit_log = json.dumps(json.loads(row.audit_log) + [dict(action='resume', actor=actor, reason=reason)])
        db.commit()
        return info(row)

    return dict(SourceGeneration=SourceGeneration, Gap=Gap, source_context=source_context,
                open_epoch=open_epoch, seal_epoch=seal_epoch, resume_epoch=resume_epoch)
