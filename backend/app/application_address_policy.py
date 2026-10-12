"""Persistent policy and validation for user-configured application addresses."""
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select


SETTING_KEY = "application_integration_allow_http"


class PolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_http: StrictBool


def install(ns):
    def read_allow_http(db):
        row = db.scalar(select(ns["SystemSetting"]).where(
            ns["SystemSetting"].setting_key == SETTING_KEY
        ))
        if not row:
            return False
        return str(row.setting_value).strip().lower() in {"true", '"true"', "1"}

    def allow_http(db=None):
        if db is not None:
            return read_allow_http(db)
        with ns["SessionLocal"]() as session:
            return read_allow_http(session)

    def validate_url(value, db=None, allow_empty=False):
        if value == "" and allow_empty:
            return value
        if not isinstance(value, str) or not value:
            raise ValueError("Application address must be an HTTP or HTTPS URL")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Application address must be a valid HTTP or HTTPS URL") from exc
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or port == 0 or parsed.fragment or "#" in value or "\\" in value
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            raise ValueError("Application address must be a valid HTTP or HTTPS URL")
        if parsed.scheme.lower() == "http" and not allow_http(db):
            raise ValueError("Application addresses must use HTTPS")
        return value

    app = ns["app"]

    @app.get("/api/settings/application-address-policy")
    def get_application_address_policy(user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        return {"allow_http": allow_http(db)}

    @app.get("/api/admin/settings/application-address-policy")
    def admin_get_application_address_policy(user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        ns["require_platform_admin"](user)
        return {"allow_http": allow_http(db)}

    @app.patch("/api/admin/settings/application-address-policy")
    def admin_patch_application_address_policy(body: PolicyBody,
                                                user=Depends(ns["current_user"]),
                                                db=Depends(ns["db_session"])):
        ns["require_platform_admin"](user)
        before = {"allow_http": allow_http(db)}
        row = db.scalar(select(ns["SystemSetting"]).where(
            ns["SystemSetting"].setting_key == SETTING_KEY
        ))
        if row is None:
            row = ns["SystemSetting"](setting_key=SETTING_KEY)
            db.add(row)
        row.setting_value = "true" if body.allow_http else "false"
        row.is_secret = False
        row.updated_by = user.email or user.id
        after = {"allow_http": body.allow_http}
        ns["audit"](db, user.email or user.id, "update_application_address_policy",
                    "system_setting", SETTING_KEY, category="security",
                    before=before, after=after)
        db.commit()
        return after

    return {"allow_http": allow_http, "validate_url": validate_url}
