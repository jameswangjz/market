"""Dameng schema adaptations for the SQLAlchemy 2.0 models."""

from hashlib import sha256

from sqlalchemy import UniqueConstraint, false, func, true
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, False_, True_


@compiles(BinaryExpression, "dm")
def compile_boolean_identity(expression, compiler, **kwargs):
    # The dialect emits IS 1/IS 0, which this DM build rejects.
    if expression.operator in (operators.is_, operators.is_not) and isinstance(expression.right, (True_, False_)):
        fallback = false() if isinstance(expression.right, True_) else true()
        value = func.coalesce(expression.left, fallback)
        comparison = value == expression.right if expression.operator == operators.is_ else value != expression.right
        return compiler.visit_binary(comparison, **kwargs)
    return compiler.visit_binary(expression, **kwargs)


def prepare_metadata(metadata):
    """Name unnamed constraints so the DM dialect never repeats long table names."""
    for table in metadata.tables.values():
        for column in table.columns:
            if column.name.lower() == "rowid":
                # ROWID is a DM pseudocolumn; preserve the ORM attribute key.
                column.name = "market_rowid"
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.name is None:
                identity = "/".join((table.fullname, *(column.name for column in constraint.columns)))
                constraint.name = "uq_dm_" + sha256(identity.encode()).hexdigest()[:24]


def ensure_indexes(engine):
    """Preserve the predicates of PostgreSQL's filtered uniqueness in DM."""
    active_states = ("pending_payment", "resource_pending", "retrying", "compensating", "refund_pending", "exception")
    arms = " ".join(f"WHEN '{state}' THEN combination_id" for state in active_states)
    definitions = (
        "CREATE INDEX IF NOT EXISTS ix_settlement_measurements_event_key ON settlement_measurements(event_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dm_settlement_rules_active ON settlement_rules"
        " (CASE WHEN status = 'active' THEN status ELSE NULL END)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dm_sla_gateway_source_seq ON sla_gateway_terminal_events"
        " (CASE WHEN sequence > 0 THEN producer_id ELSE NULL END,"
        " CASE WHEN sequence > 0 THEN window_slot ELSE NULL END,"
        " CASE WHEN sequence > 0 THEN sequence ELSE NULL END)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dm_version_change_active ON version_change_requests"
        f" (CASE status {arms} ELSE NULL END)",
    )
    with engine.begin() as connection:
        for ddl in definitions:
            connection.exec_driver_sql(ddl)


def install_transaction_locks(engine):
    """Release DM session locks only after the owning connection has ended its transaction."""
    from sqlalchemy import event

    @event.listens_for(engine.pool, "reset")
    def release_locks(dbapi_connection, record, state):
        held = record.info.pop("market_dm_transaction_locks", {})
        if not held or state.terminate_only:
            return
        # Even read-only transactions keep DBMS_LOCK on this DM build unless released explicitly.
        dbapi_connection.rollback()
        cursor = dbapi_connection.cursor()
        try:
            for lock_id in held:
                cursor.execute("SELECT SYS.DBMS_LOCK.RELEASE(?) FROM DUAL", [lock_id])
                if cursor.fetchone()[0] not in (0, 4):
                    raise RuntimeError("Failed to release the Market database lock")
        finally:
            cursor.close()
        dbapi_connection.rollback()


def transaction_lock(connection, key, *, shared=False, timeout=10):
    from sqlalchemy import text

    identity = f"market:{connection.dialect.default_schema_name}:{key}"
    lock_id = 1 + int.from_bytes(sha256(identity.encode()).digest()[:8], "big") % 999_999_999
    mode = 4 if shared else 6
    held = connection.info.setdefault("market_dm_transaction_locks", {})
    if lock_id in held:
        if held[lock_id] in (mode, 6):
            return True
        raise RuntimeError("Cannot upgrade a shared Market database lock")
    result = connection.scalar(text("SELECT SYS.DBMS_LOCK.REQUEST(:lock_id, :mode, :timeout, FALSE) FROM DUAL"),
                               {"lock_id": lock_id, "mode": mode, "timeout": timeout})
    if result != 0:
        if result == 1 and timeout == 0:
            return False
        raise RuntimeError(f"Failed to acquire the Market database lock (DM result {result})")
    held[lock_id] = mode
    return True
