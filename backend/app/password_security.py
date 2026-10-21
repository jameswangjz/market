"""Account password changes and verified-email recovery; no development bypass."""
import hashlib
import hmac
import secrets
import smtplib
import ssl
from datetime import timedelta, timezone
from email.message import EmailMessage

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, Integer, String, select


class ChangePasswordBody(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class RequestResetBody(BaseModel):
    identifier: str = Field(min_length=1, max_length=255)


class ResetPasswordBody(RequestResetBody):
    code: str = Field(pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=8, max_length=128)


def install(ns):
    Base, User = ns['Base'], ns['User']
    now, db_session, current_user = ns['now'], ns['db_session'], ns['current_user']

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    class PasswordSecurity(Base):
        __tablename__ = 'password_security'
        user_id = Column(String(64), primary_key=True)
        changed_at = Column(DateTime(timezone=True), nullable=True)
        requested_at = Column(DateTime(timezone=True), nullable=True)
        expires_at = Column(DateTime(timezone=True), nullable=True)
        code_hash = Column(String(64), default='')
        attempts = Column(Integer, default=0)
        target = Column(String(255), default='')
        change_attempts = Column(Integer, default=0)
        change_locked_until = Column(DateTime(timezone=True), nullable=True)

    def allowed(user):
        return (user and user.is_active and user.activation_status == 'active'
                and user.username != 'ywsh' and user.platform_role != 'business_reviewer')

    def find(db, identifier):
        value = identifier.strip().lower()
        return db.scalar(select(User).where(ns['or_'](User.username == value,
            User.email == value, User.phone == value)).with_for_update().execution_options(populate_existing=True))

    def state(db, user):
        result = db.get(PasswordSecurity, user.id, populate_existing=True)
        if result is None:
            result = PasswordSecurity(user_id=user.id)
            db.add(result)
            db.flush()
        return result

    def validate(password):
        if (not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password)
                or password.isspace()):
            raise HTTPException(400, '新密码需为8至128位，至少包含字母和数字')

    def replace(db, user, security, password, action):
        validate(password)
        if ns['verify_password'](password, user.password_hash):
            raise HTTPException(400, '新密码不能与原密码相同')
        user.password_hash = ns['hash_password'](password)
        user.temporary_password_hash = ''
        user.temporary_password_expires_at = None
        security.changed_at = now()
        security.code_hash = ''
        security.expires_at = None
        security.change_attempts = 0
        security.change_locked_until = None
        ns['audit'](db, user.email or user.phone or user.id, action, 'user', user.id,
                    '密码已更新，原登录凭据失效')
        db.commit()
        return {'message': '密码已更新，请重新登录'}

    @ns['app'].post('/api/auth/change-password')
    def change(body: ChangePasswordBody, user=Depends(current_user), db=Depends(db_session)):
        user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
        security = state(db, user)
        if security.change_locked_until and aware(security.change_locked_until) > now():
            raise HTTPException(429, '原密码错误次数过多，请10分钟后重试')
        if security.change_locked_until:
            security.change_attempts = 0
            security.change_locked_until = None
        if not ns['verify_password'](body.current_password, user.password_hash):
            security.change_attempts += 1
            if security.change_attempts >= 5:
                security.change_locked_until = now() + timedelta(minutes=10)
            db.commit()
            raise HTTPException(400, '原密码不正确')
        return replace(db, user, security, body.new_password, 'change_password')

    @ns['app'].post('/api/auth/password-reset/request')
    def request_reset(body: RequestResetBody, db=Depends(db_session)):
        response = {'message': '如账号已绑定并验证邮箱，重置验证码将发送至该邮箱，10分钟内有效；请勿频繁请求'}
        user = find(db, body.identifier)
        if not allowed(user) or not user.email or not user.email_verified:
            return response
        security = state(db, user)
        if security.requested_at and aware(security.requested_at) > now() - timedelta(minutes=10):
            return response
        values = {s.setting_key: s.setting_value for s in db.scalars(select(ns['SystemSetting']))}
        if not all(values.get(k) for k in ('smtp_host', 'smtp_username', 'smtp_password')):
            return response
        code = f'{secrets.randbelow(1000000):06d}'
        security.code_hash = hmac.new(ns['JWT_SECRET'].encode(), f'{user.id}:{code}'.encode(), hashlib.sha256).hexdigest()
        security.requested_at = now()
        security.expires_at = now() + timedelta(minutes=10)
        security.attempts = 0
        security.target = user.email
        db.commit()
        message = EmailMessage()
        message['Subject'] = '数据集运营服务管理平台 - 重置登录密码'
        message['From'] = values['smtp_username']
        message['To'] = user.email
        message.set_content(f'您好，\n\n您的密码重置验证码：{code}\n验证码10分钟内有效，仅可使用一次。请勿向他人提供验证码。\n如非本人操作，请忽略此邮件，原密码不会改变。')
        try:
            use_ssl = values.get('smtp_ssl') == 'true'
            factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
            kwargs = {'context': ssl.create_default_context()} if use_ssl else {}
            with factory(values['smtp_host'], int(values.get('smtp_port', '587')), timeout=15, **kwargs) as server:
                if not use_ssl and values.get('smtp_starttls', 'true') == 'true':
                    server.starttls(context=ssl.create_default_context())
                server.login(values['smtp_username'], values['smtp_password'])
                server.send_message(message)
        except Exception:
            ns['audit'](db, 'system', 'password_reset_email_failed', 'user', user.id, '重置邮件发送失败，请检查SMTP配置')
            db.commit()
        return response

    @ns['app'].post('/api/auth/password-reset/confirm')
    def confirm(body: ResetPasswordBody, db=Depends(db_session)):
        user = find(db, body.identifier)
        security = db.get(PasswordSecurity, user.id) if user else None
        if (not allowed(user) or not security or not security.code_hash
                or not security.expires_at or aware(security.expires_at) <= now()
                or security.attempts >= 5 or not user.email_verified or security.target != user.email):
            raise HTTPException(400, '验证码无效或已过期')
        candidate = hmac.new(ns['JWT_SECRET'].encode(), f'{user.id}:{body.code}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(candidate, security.code_hash):
            security.attempts += 1
            db.commit()
            raise HTTPException(400, '验证码无效或已过期')
        return replace(db, user, security, body.new_password, 'reset_password')

    ns['PasswordSecurity'] = PasswordSecurity
    return PasswordSecurity
