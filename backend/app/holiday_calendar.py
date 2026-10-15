"""Versioned CN holiday overrides from one fixed, bounded HTTPS source."""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import secrets
import time
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException, Query
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column


SOURCE = 'https://raw.githubusercontent.com/cg-zhou/holiday-calendar/main/data/CN/{year}.json'
MIN_YEAR, MAX_YEAR = 2000, 2100
MAX_BYTES = 128 * 1024
MAX_SECONDS = 15


def validate_year(year):
    if type(year) is not int or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError('Calendar year must be an integer between 2000 and 2100')
    return year


def normalized_document(document, year):
    validate_year(year)
    if (not isinstance(document, dict) or type(document.get('year')) is not int
            or document['year'] != year or document.get('region') != 'CN'):
        raise ValueError('Calendar year or region mismatch')
    entries = document.get('dates')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 366:
        raise ValueError('A nonempty bounded annual calendar is required')
    dates, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('date'), str):
            raise ValueError('Invalid calendar date')
        parsed = date.fromisoformat(entry['date'])
        if parsed.year != year or parsed.isoformat() != entry['date'] or parsed in seen:
            raise ValueError('Duplicate or out-of-year calendar date')
        if entry.get('type') not in {'public_holiday', 'transfer_workday'}:
            raise ValueError('Unknown calendar date type')
        name = entry.get('name', '')
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError('Invalid calendar date name')
        seen.add(parsed)
        dates.append({'date': entry['date'], 'type': entry['type'], 'name': name})
    return {'year': year, 'region': 'CN', 'dates': sorted(dates, key=lambda entry: entry['date'])}


def canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class FetchFailure(Exception):
    def __init__(self, reason, status='error'):
        self.reason, self.status = reason, status


def fetch_document(year):
    """No caller-supplied host, redirects, proxies, compression, or unbounded body."""
    url = SOURCE.format(year=validate_year(year))
    started = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False) as client:
            with client.stream('GET', url, headers={'Accept': 'application/json',
                    'Accept-Encoding': 'identity'}) as response:
                if response.status_code == 404:
                    raise FetchFailure('http_404', 'notpublished')
                if response.status_code != 200:
                    raise FetchFailure('unexpected_http_status')
                if response.headers.get('content-encoding', 'identity').lower() not in {'', 'identity'}:
                    raise FetchFailure('encoded_body_rejected')
                length = response.headers.get('content-length')
                if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                    raise FetchFailure('body_too_large_or_invalid_length')
                body = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() - started > MAX_SECONDS:
                        raise FetchFailure('total_timeout')
                    if len(body) + len(chunk) > MAX_BYTES:
                        raise FetchFailure('body_too_large')
                    body.extend(chunk)
                return normalized_document(json.loads(body), year)
    except httpx.TimeoutException as exc:
        raise FetchFailure('timeout') from exc
    except httpx.HTTPError as exc:
        raise FetchFailure('transport_error') from exc
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise FetchFailure('invalid_document') from exc


def install(ns):
    Base = ns['Base']

    class CalendarVersion(Base):
        __tablename__ = 'holiday_calendar_versions'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        year = mapped_column(Integer, nullable=False, index=True)
        region = mapped_column(String(2), nullable=False, default='CN')
        content_hash = mapped_column(String(64), nullable=False)
        source_url = mapped_column(String(300), nullable=False)
        fetched_at = mapped_column(DateTime(timezone=True), nullable=False)
        document_json = mapped_column(Text, nullable=False)
        __table_args__ = (UniqueConstraint('year', 'region', 'content_hash', name='uq_holiday_calendar_content'),)

    class CalendarState(Base):
        __tablename__ = 'holiday_calendar_states'
        year = mapped_column(Integer, primary_key=True)
        current_version_id = mapped_column(ForeignKey('holiday_calendar_versions.id'), nullable=True)
        checked_at = mapped_column(DateTime(timezone=True), nullable=False)
        sync_status = mapped_column(String(30), nullable=False)
        reason = mapped_column(String(80), nullable=False, default='')

    def locked_state(db, year):
        query = select(CalendarState).where(CalendarState.year == year).with_for_update().execution_options(
            populate_existing=True)
        state = db.scalar(query)
        if state is None:
            try:
                with db.begin_nested():
                    state = CalendarState(year=year, checked_at=ns['now'](), sync_status='never')
                    db.add(state)
                    db.flush()
            except IntegrityError:
                state = db.scalar(query)
        return state

    def checked_document(version):
        if version is None or version.region != 'CN' or version.source_url != SOURCE.format(year=version.year):
            return None
        try:
            document = normalized_document(json.loads(version.document_json), version.year)
            if hashlib.sha256(canonical(document).encode()).hexdigest() != version.content_hash:
                return None
            return document
        except (ValueError, TypeError, RecursionError):
            return None

    def version_metadata(version):
        stamp = version.fetched_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return {'id': version.id, 'year': version.year, 'region': version.region,
                'content_hash': version.content_hash, 'source_url': version.source_url,
                'fetched_at': stamp.isoformat()}

    def status_for(db, year):
        state = db.get(CalendarState, year, populate_existing=True)
        version = db.get(CalendarVersion, state.current_version_id, populate_existing=True) if (
            state is not None and state.current_version_id) else None
        valid = version is not None and version.year == year and checked_document(version) is not None
        history = db.scalars(select(CalendarVersion).where(CalendarVersion.year == year)
            .order_by(CalendarVersion.fetched_at.desc(), CalendarVersion.id.desc())).all()
        return {'year': year, 'cache_status': 'available' if valid else 'not_ready',
                'sync_status': state.sync_status if state is not None else 'never',
                'reason': state.reason if state is not None else '',
                'checked_at': state.checked_at if state is not None else None,
                'current_version': version_metadata(version) if valid else None,
                'versions': [version_metadata(row) for row in history]}

    def sync_year(db, year, actor='scheduler'):
        validate_year(year)
        # Fetch before taking the annual row lock; HTTP never holds this lock.
        try:
            document = fetch_document(year)
            failure = None
        except FetchFailure as exc:
            document, failure = None, exc
        state = locked_state(db, year)
        before = state.current_version_id
        state.checked_at = ns['now']()
        if failure is not None:
            state.sync_status, state.reason = failure.status, failure.reason
        else:
            raw = canonical(document)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            query = select(CalendarVersion).where(CalendarVersion.year == year,
                CalendarVersion.region == 'CN', CalendarVersion.content_hash == digest)
            version = db.scalar(query)
            if version is None:
                version = CalendarVersion(year=year, region='CN', content_hash=digest,
                    source_url=SOURCE.format(year=year), fetched_at=state.checked_at, document_json=raw)
                db.add(version)
                db.flush()
            state.sync_status = 'unchanged' if state.current_version_id == version.id else 'updated'
            state.current_version_id, state.reason = version.id, ''
        ns['audit'](db, actor, 'sync_holiday_calendar', 'holiday_calendar', str(year), state.reason,
            category='sla', business_domain='sla', before={'version_id': before},
            after={'version_id': state.current_version_id, 'sync_status': state.sync_status,
                   'source_url': SOURCE.format(year=year)})
        db.flush()
        result = status_for(db, year)
        instant = ns['now']()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        local = instant.astimezone(ZoneInfo('Asia/Shanghai'))
        if state.sync_status == 'error' or (state.sync_status == 'notpublished'
                and year == local.year and result['cache_status'] != 'available'):
            ns['notify_business_event'](db, 'SLA节假日日历同步异常',
                f'{year}年日历同步状态：{state.sync_status}；缓存状态：{result["cache_status"]}。请检查数据源。',
                'holiday_calendar', str(year),
                event_key=f'holiday-calendar:{year}:{state.sync_status}:{local.date().isoformat()}',
                category='system', platform_roles=['super_admin', 'platform_operator'])
        return result

    def enrich_support_calendar(db, base_calendar):
        calendar = deepcopy(base_calendar)
        calendar.update(holidays=[], workdays=[], covered_years=[], holiday_calendar_sources=[])
        if calendar.get('exclude_public_holidays', True) is False:
            calendar.update(holiday_calendar_status='not_required', holiday_calendar_version=None)
            return calendar
        versions = db.scalars(select(CalendarVersion).join(CalendarState,
            CalendarState.current_version_id == CalendarVersion.id).where(CalendarState.year == CalendarVersion.year)
            .order_by(CalendarVersion.year)
            .execution_options(populate_existing=True)).all()
        for version in versions:
            document = checked_document(version)
            if document is None:
                continue
            calendar['covered_years'].append(version.year)
            calendar['holidays'].extend(item['date'] for item in document['dates'] if item['type'] == 'public_holiday')
            calendar['workdays'].extend(item['date'] for item in document['dates'] if item['type'] == 'transfer_workday')
            calendar['holiday_calendar_sources'].append(version_metadata(version))
        calendar['holidays'].sort()
        calendar['workdays'].sort()
        sources = [(item['year'], item['content_hash']) for item in calendar['holiday_calendar_sources']]
        version = hashlib.sha256(canonical(sources).encode()).hexdigest() if sources else None
        calendar.update(holiday_calendar_version=version,
                        holiday_calendar_status='cached_years_only' if sources else 'unconfigured')
        if version is not None:
            calendar['version'] = hashlib.sha256(canonical([base_calendar.get('version'), version]).encode()).hexdigest()
        return calendar

    @ns['app'].get('/api/sla/holiday-calendars')
    def list_calendars(year: int | None = Query(default=None, ge=MIN_YEAR, le=MAX_YEAR),
                       user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ns['require_platform_admin'](user)
        years = [year] if year is not None else db.scalars(select(CalendarState.year).order_by(CalendarState.year)).all()
        return {'items': [status_for(db, value) for value in years]}

    @ns['app'].post('/api/sla/holiday-calendars/{year}/sync')
    def synchronize(year: int, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ns['require_platform_admin'](user)
        try:
            validate_year(year)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = sync_year(db, year, user.email or user.id)
        db.commit()
        return result

    hooks = {'Version': CalendarVersion, 'State': CalendarState, 'sync_year': sync_year,
             'status_for': status_for, 'enrich_support_calendar': enrich_support_calendar}
    ns['holiday_calendar'] = hooks
    ns['enrich_support_calendar'] = enrich_support_calendar
    return hooks


def run_years(runtime, years):
    """Independent transactions preserve this year's success if next year fails."""
    failed = False
    for year in years:
        with runtime.SessionLocal() as db:
            result = runtime.holiday_calendar['sync_year'](db, validate_year(year))
            db.commit()
        print(json.dumps({'year': year, 'sync_status': result['sync_status'],
                          'cache_status': result['cache_status'], 'reason': result['reason']}))
        failed = failed or result['sync_status'] == 'error'
    return int(failed)


def main():
    import os
    os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')
    from app import main as runtime
    year = datetime.now(ZoneInfo('Asia/Shanghai')).year
    return run_years(runtime, (year, year + 1))


if __name__ == '__main__':
    raise SystemExit(main())
