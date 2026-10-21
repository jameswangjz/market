"""Enterprise-scoped SaaS user and department synchronization."""

import hashlib
import json
from datetime import timezone
from typing import Literal

from fastapi import Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator
from sqlalchemy import select
from .saas_reconciliation import reconciliation_context


class SaaSSyncActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "remove"]
    kind: Literal["users", "departments"]
    ids: list[StrictStr] = Field(default_factory=list, max_length=10)
    all: StrictBool = False

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not value or value != value.strip() or len(value) > 180 for value in values):
            raise ValueError("IDs must be nonempty, unpadded strings of at most 180 characters")
        return values


def install(ns):
    app = ns["app"]
    select = ns["select"]
    User, Membership = ns["User"], ns["Membership"]
    Department, MembershipDepartment = ns["EnterpriseDepartment"], ns["MembershipDepartment"]
    Subscription = ns["SaaSSubscription"]
    UserMapping, DepartmentMapping = ns["SaaSUserMapping"], ns["SaaSDepartment"]
    max_items = 10

    def execute_sync(db, subscription, operation, payload, config, key):
        Model = ns['SaaSOperation']
        existing = db.scalar(select(Model).where(Model.idempotency_key == key))
        if existing:
            if existing.subscription_id != subscription.id or existing.operation != operation:
                raise HTTPException(409, 'SaaS operation identity conflict')
            payload = json.loads(existing.request_payload)
        else:
            payload = dict(payload, reconciliation_context=reconciliation_context(
                subscription_expires_at=subscription.expires_at,
                provisional=subscription.expires_at is None and subscription.billing_cycle != 'perpetual',
                effective_at_semantics='provider_receipt', event_id=key, event_type=operation,
                product_id=subscription.product_id, enterprise_id=subscription.enterprise_id,
                subscription_id=subscription.id, tenant_id=subscription.external_tenant_id))
        return ns['execute_saas_operation'](db, subscription, operation, payload, config, key)

    def context(subscription_id, user, db, *, provider=False):
        if user.platform_role or not user.is_active or user.activation_status != "active" or user.verified_status != "verified":
            raise HTTPException(403, "仅已实名且正常启用的企业管理员可管理 SaaS 同步")
        subscription = db.get(Subscription, subscription_id)
        if not subscription:
            raise HTTPException(404, "SaaS 订阅不存在")
        membership = ns["require_enterprise_admin"](db, user, subscription.enterprise_id)
        enterprise = db.get(ns["Enterprise"], subscription.enterprise_id)
        if not enterprise or enterprise.verification_status != "verified":
            raise HTTPException(403, "仅已实名且正常启用的购买企业可管理 SaaS 同步")
        current = ns["now"]()
        starts = subscription.starts_at
        expires = subscription.expires_at
        if starts and starts.tzinfo is None:
            starts = starts.replace(tzinfo=timezone.utc)
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if subscription.status != "active" or (starts and starts > current) or (expires and expires <= current):
            raise HTTPException(409, "SaaS 订阅当前无效或未生效")
        product = ns["saas_product"](subscription.product_id, db)
        config = db.scalar(select(ns["SaaSIntegrationConfig"]).where(
            ns["SaaSIntegrationConfig"].product_id == product.id,
            ns["SaaSIntegrationConfig"].status == "active"))
        if not config:
            raise HTTPException(400, "SaaS 接口配置不存在")
        return subscription, membership, product, config

    def department_rows(db, enterprise_id):
        return db.scalars(select(Department).where(
            Department.enterprise_id == enterprise_id, Department.status == "active"
        ).order_by(Department.created_at, Department.id)).all()

    def user_rows(db, enterprise_id):
        return db.execute(select(User, Membership).join(Membership, Membership.user_id == User.id).where(
            Membership.enterprise_id == enterprise_id, Membership.status == "active",
            User.is_active.is_(True), User.activation_status == "active", User.verified_status == "verified",
            User.platform_role == ""
        ).order_by(User.created_at, User.id)).all()

    def active_department_ids(db, membership):
        values = db.scalars(select(MembershipDepartment.department_id).where(
            MembershipDepartment.membership_id == membership.id)).all()
        if membership.department_id:
            values.insert(0, membership.department_id)
        return list(dict.fromkeys(values))

    def assigned_payload(db, subscription_id, user_id):
        operations = db.scalars(select(ns["SaaSOperation"]).where(
            ns["SaaSOperation"].subscription_id == subscription_id,
            ns["SaaSOperation"].operation == "USER_ASSIGN",
            ns["SaaSOperation"].status == "succeeded").order_by(
                ns["SaaSOperation"].completed_at.desc(), ns["SaaSOperation"].created_at.desc())).all()
        for operation in operations:
            payload = json.loads(operation.request_payload or "{}")
            if payload.get("user_id") == user_id:
                return payload
        return {}

    def operation_key(db, subscription_id, operation, target_id, fingerprint=""):
        # A new inverse transition starts a new lifecycle; retries reuse its key.
        inverse = {"USER_ASSIGN": "USER_UNASSIGN", "USER_UNASSIGN": "USER_ASSIGN",
                   "DEPT_CREATE": "DEPT_REMOVE", "DEPT_REMOVE": "DEPT_CREATE"}[operation]
        rows = db.scalars(select(ns["SaaSOperation"]).where(
            ns["SaaSOperation"].subscription_id == subscription_id,
            ns["SaaSOperation"].operation == inverse,
            ns["SaaSOperation"].status == "succeeded")).all()
        generation = sum(f":{target_id}:" in row.idempotency_key for row in rows)
        if operation == "USER_ASSIGN":
            assignments = db.scalars(select(ns["SaaSOperation"]).where(
                ns["SaaSOperation"].subscription_id == subscription_id,
                ns["SaaSOperation"].operation == operation,
                ns["SaaSOperation"].status == "succeeded").order_by(
                    ns["SaaSOperation"].completed_at.desc(), ns["SaaSOperation"].created_at.desc())).all()
            assignments = [row for row in assignments if json.loads(row.request_payload or "{}").get("user_id") == target_id]
            prefix = f"sync:{subscription_id}:{operation}:{target_id}:{generation}:"
            if assignments and assignments[0].idempotency_key.startswith(prefix) and assignments[0].idempotency_key.endswith(f":{fingerprint}"):
                return assignments[0].idempotency_key
            return f"{prefix}{len(assignments)}:{fingerprint}"
        return f"sync:{subscription_id}:{operation}:{target_id}:{generation}:{fingerprint}"

    @app.get("/api/saas-subscriptions/{subscription_id}/sync-tree")
    def sync_tree(subscription_id: str, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        subscription, _, _, _ = context(subscription_id, user, db)
        departments = department_rows(db, subscription.enterprise_id)
        enterprise_department_ids = {item.id for item in departments}
        rows = user_rows(db, subscription.enterprise_id)
        user_mappings = db.scalars(select(UserMapping).where(
            UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all()
        department_mappings = db.scalars(select(DepartmentMapping).where(
            DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()
        mapped_users = {item.user_id: item.id for item in user_mappings}
        mapped_departments = {item.platform_department_id: item.id for item in department_mappings}
        children = {}
        for item in departments:
            children.setdefault(item.parent_id or "", []).append(item)
        members_by_department = {}
        unassigned = []
        for target, membership in rows:
            node = {"id": target.id, "name": target.name, "email": target.email or "",
                    "mapped": target.id in mapped_users}
            member_depts = [value for value in active_department_ids(db, membership) if value in enterprise_department_ids]
            for department_id in member_depts:
                members_by_department.setdefault(department_id, []).append(node)
            if not member_depts:
                unassigned.append(node)

        def build(department, ancestry):
            if department.id in ancestry:
                return None
            branch = ancestry | {department.id}
            return {"id": department.id, "name": department.name,
                    "mapped": department.id in mapped_departments,
                    "users": members_by_department.get(department.id, []),
                    "children": [built for child in children.get(department.id, [])
                                 if (built := build(child, branch)) is not None]}

        tree = [built for item in children.get("", []) if (built := build(item, set())) is not None]
        known = {item.id for item in departments}
        for department in departments:
            if department.parent_id and department.parent_id not in known:
                built = build(department, set())
                if built:
                    tree.append(built)
        mapped_user_rows = []
        member_by_id = {target.id: (target, membership) for target, membership in rows}
        for mapping in user_mappings:
            target, _membership = member_by_id.get(mapping.user_id, (None, None))
            mapped_user_rows.append({"id": mapping.id, "name": target.name if target else "已离职或不可用成员",
                "email": (target.email or "") if target else "", "type": "user",
                "platform_user_id": mapping.user_id, "mapped": True,
                "department_id": mapping.department_id or "",
                "department_ids": assigned_payload(db, subscription.id, mapping.user_id).get("department_ids", [])})
        mapped_department_rows = [{"id": mapping.id, "name": mapping.name,
            "type": "department", "platform_department_id": mapping.platform_department_id,
            "mapped": True, "parent_mapping_id": mapping.parent_id or "",
            "external_department_id": mapping.external_department_id or mapping.id}
            for mapping in department_mappings]
        return {"departments": tree, "unassigned_users": unassigned,
                "mapped_users": mapped_user_rows, "mapped_departments": mapped_department_rows,
                "mappings": {"users": sorted(mapped_users), "departments": sorted(mapped_departments)}}

    @app.post("/api/saas-subscriptions/{subscription_id}/sync-actions")
    def sync_actions(subscription_id: str, body: SaaSSyncActionBody = Body(...), user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        subscription, _, _, config = context(subscription_id, user, db)
        action, kind = body.action, body.kind
        ids, all_items = body.ids, body.all
        if all_items == bool(ids):
            raise HTTPException(400, "请提供 ids 或 all=true")

        dept_by_id = {item.id: item for item in department_rows(db, subscription.enterprise_id)}
        all_users = {target.id: (target, membership) for target, membership in user_rows(db, subscription.enterprise_id)}
        if kind == "users":
            candidates = list(all_users) if all_items and action == "add" else (
                [item.id for item in db.scalars(select(UserMapping).where(
                    UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all()]
                if all_items else ids)
            if any(value not in all_users for value in candidates):
                allowed = {m.user_id for m in db.scalars(select(UserMapping).where(
                    UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all()}
                allowed.update(m.id for m in db.scalars(select(UserMapping).where(
                    UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all())
                if not (action == "remove" and all(value in allowed for value in candidates)):
                    raise HTTPException(404, "所选用户不属于当前企业或不可用")
        else:
            candidates = list(dept_by_id) if all_items and action == "add" else (
                [item.id for item in db.scalars(select(DepartmentMapping).where(
                    DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()]
                if all_items else ids)
            if any(value not in dept_by_id for value in candidates):
                allowed = {m.platform_department_id for m in db.scalars(select(DepartmentMapping).where(
                    DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()}
                allowed.update(m.id for m in db.scalars(select(DepartmentMapping).where(
                    DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all())
                if not (action == "remove" and all(value in allowed for value in candidates)):
                    raise HTTPException(404, "所选部门不属于当前企业或不可用")

        if len(candidates) > max_items:
            raise HTTPException(413, f"单次同步最多处理 {max_items} 项，请分批选择")

        if len(set(candidates)) != len(candidates):
            candidates = list(dict.fromkeys(candidates))
        if kind == "departments":
            mapping_by_id = {item.id: item for item in db.scalars(select(DepartmentMapping).where(
                DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()}
            def depth(item_id):
                result, seen, current = 0, set(), dept_by_id.get(item_id)
                if action == "remove" and item_id in mapping_by_id:
                    current = mapping_by_id[item_id]
                    while current and current.parent_id and current.parent_id in mapping_by_id and current.id not in seen:
                        seen.add(current.id); result += 1; current = mapping_by_id[current.parent_id]
                    return result
                while current and current.parent_id and current.parent_id in dept_by_id and current.id not in seen:
                    seen.add(current.id); result += 1; current = dept_by_id[current.parent_id]
                return result
            candidates.sort(key=depth, reverse=action == "remove")
        results = []
        actor = user.email or user.phone or user.id
        active_user_maps = {item.user_id: item for item in db.scalars(select(UserMapping).where(
            UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all()}
        active_dept_maps = {item.platform_department_id: item for item in db.scalars(select(DepartmentMapping).where(
            DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()}
        for target_id in candidates:
            label = "用户" if kind == "users" else "部门"
            previous = (active_user_maps if kind == "users" else active_dept_maps).get(target_id)
            if not previous:
                mapping_type = UserMapping if kind == "users" else DepartmentMapping
                previous = db.scalar(select(mapping_type).where(mapping_type.id == target_id,
                    mapping_type.subscription_id == subscription.id))
            before = {"status": previous.status if previous else "unsynced", "target_id": target_id}
            if kind == "users" and previous:
                before["assignment"] = assigned_payload(db, subscription.id, previous.user_id)
            try:
                if kind == "users":
                    target, membership = all_users.get(target_id, (None, None))
                    if action == "add":
                        existing = db.scalar(select(UserMapping).where(UserMapping.subscription_id == subscription.id,
                            UserMapping.user_id == target_id))
                        platform_depts = [value for value in active_department_ids(db, membership) if value in dept_by_id]
                        mappings = {item.platform_department_id: item for item in db.scalars(select(DepartmentMapping).where(
                            DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active")).all()}
                        external_depts = [mappings[value].external_department_id or mappings[value].id
                                          for value in platform_depts if value in mappings]
                        payload = {"tenant_id": subscription.external_tenant_id, "user_id": target.id,
                            "username": target.email or target.phone or target.id, "email": target.email or "",
                            "name": target.name, "department_ids": external_depts,
                            "department_id": external_depts[0] if external_depts else ""}
                        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
                        key = operation_key(db, subscription.id, "USER_ASSIGN", target.id, payload_hash)
                        provider_result = execute_sync(db, subscription, "USER_ASSIGN", payload, config, key)
                        mapping = existing or UserMapping(subscription_id=subscription.id, user_id=target.id)
                        db.add(mapping)
                        mapping.department_id = platform_depts[0] if platform_depts else ""
                        mapping.external_user_id = str(provider_result.get("external_user_id", provider_result.get("user_id", target.id)))
                        mapping.status = "active"
                        db.flush()
                        result_id = mapping.id
                    else:
                        mapping = active_user_maps.get(target_id)
                        if not mapping:
                            mapping = db.scalar(select(UserMapping).where(UserMapping.id == target_id,
                                UserMapping.subscription_id == subscription.id, UserMapping.status == "active"))
                        if not mapping:
                            ns["audit"](db, actor, f"saas_sync_{action}_{kind[:-1]}", "saas_sync", target_id,
                                "SaaS 用户未同步，无需移除", category="delivery", business_domain="saas",
                                tenant_id=subscription.enterprise_id, after={"status": "skipped"})
                            db.commit()
                            results.append({"id": target_id, "status": "skipped", "reason": "not_synced"}); continue
                        target_id = mapping.user_id
                        operations = db.scalars(select(ns["SaaSOperation"]).where(
                            ns["SaaSOperation"].subscription_id == subscription.id,
                            ns["SaaSOperation"].status == "succeeded")).all()
                        if any(json.loads(item.request_payload or "{}").get("admin_user_id") == target_id
                               for item in operations):
                            raise HTTPException(409, "不能移除 SaaS 主管理员")
                        membership = db.scalar(select(Membership).where(Membership.user_id == mapping.user_id,
                            Membership.enterprise_id == subscription.enterprise_id, Membership.status == "active"))
                        if membership and membership.role in {"super_admin", "enterprise_admin"}:
                            other_admin = db.scalar(select(UserMapping.id).join(Membership, Membership.user_id == UserMapping.user_id).where(
                                UserMapping.subscription_id == subscription.id, UserMapping.status == "active",
                                UserMapping.id != mapping.id, Membership.enterprise_id == subscription.enterprise_id,
                                Membership.status == "active", Membership.role.in_(["super_admin", "enterprise_admin"]),
                                UserMapping.user_id.in_(list(all_users))).limit(1))
                            if not other_admin:
                                raise HTTPException(409, "不能移除本企业最后一个已同步管理员")
                        provider_result = execute_sync(db, subscription, "USER_UNASSIGN",
                            {"tenant_id": subscription.external_tenant_id, "user_id": mapping.external_user_id or target_id}, config,
                            operation_key(db, subscription.id, "USER_UNASSIGN", target_id))
                        mapping.status = "deleted"; result_id = mapping.id
                else:
                    department = dept_by_id.get(target_id)
                    if action == "add":
                        existing = db.scalar(select(DepartmentMapping).where(DepartmentMapping.subscription_id == subscription.id,
                            DepartmentMapping.platform_department_id == target_id))
                        if existing and existing.status == "active":
                            ns["audit"](db, actor, f"saas_sync_{action}_{kind[:-1]}", "saas_sync", existing.id,
                                "SaaS 部门已同步，无需重复添加", category="delivery", business_domain="saas",
                                tenant_id=subscription.enterprise_id, after={"status": "skipped", "target_id": target_id})
                            db.commit()
                            results.append({"id": target_id, "status": "skipped", "reason": "already_synced"}); continue
                        parent = active_dept_maps.get(department.parent_id)
                        if department.parent_id and department.parent_id in dept_by_id and not parent:
                            raise HTTPException(409, "请先同步上级部门")
                        row = existing or DepartmentMapping(subscription_id=subscription.id,
                            platform_department_id=department.id, name=department.name, status="syncing")
                        db.add(row); db.flush()
                        provider_result = execute_sync(db, subscription, "DEPT_CREATE",
                            {"tenant_id": subscription.external_tenant_id, "department_id": row.id,
                             "parent_id": (parent.external_department_id or parent.id) if parent else "", "name": department.name},
                            config, operation_key(db, subscription.id, "DEPT_CREATE", department.id))
                        row.external_department_id = str(provider_result.get("external_department_id", provider_result.get("department_id", row.id)))
                        row.name = department.name; row.parent_id = parent.id if parent else ""; row.status = "active"
                        db.flush(); result_id = row.id
                        active_dept_maps[target_id] = row
                    else:
                        mapping = active_dept_maps.get(target_id)
                        if not mapping:
                            # Removed department IDs may be selected again for an idempotent replay.
                            mapping = db.scalar(select(DepartmentMapping).where(DepartmentMapping.subscription_id == subscription.id,
                                DepartmentMapping.platform_department_id == target_id, DepartmentMapping.status == "active"))
                        if not mapping:
                            mapping = db.scalar(select(DepartmentMapping).where(DepartmentMapping.id == target_id,
                                DepartmentMapping.subscription_id == subscription.id, DepartmentMapping.status == "active"))
                        if not mapping:
                            ns["audit"](db, actor, f"saas_sync_{action}_{kind[:-1]}", "saas_sync", target_id,
                                "SaaS 部门未同步，无需移除", category="delivery", business_domain="saas",
                                tenant_id=subscription.enterprise_id, after={"status": "skipped"})
                            db.commit()
                            results.append({"id": target_id, "status": "skipped", "reason": "not_synced"}); continue
                        target_id = mapping.platform_department_id
                        child = db.scalar(select(DepartmentMapping.id).where(DepartmentMapping.subscription_id == subscription.id,
                            DepartmentMapping.parent_id == mapping.id, DepartmentMapping.status == "active").limit(1))
                        dependent_user = False
                        for mapped_user in db.scalars(select(UserMapping).where(
                                UserMapping.subscription_id == subscription.id, UserMapping.status == "active")).all():
                            member = db.scalar(select(Membership).where(Membership.user_id == mapped_user.user_id,
                                Membership.enterprise_id == subscription.enterprise_id, Membership.status == "active"))
                            payload = assigned_payload(db, subscription.id, mapped_user.user_id)
                            remote_ids = payload.get("department_ids", [payload.get("department_id", "")])
                            if (mapped_user.department_id == target_id
                                    or (mapping.external_department_id or mapping.id) in remote_ids
                                    or (member and target_id in active_department_ids(db, member))):
                                dependent_user = True
                                break
                        if child or dependent_user:
                            raise HTTPException(409, "部门仍有已同步的子部门或用户")
                        provider_result = execute_sync(db, subscription, "DEPT_REMOVE",
                            {"tenant_id": subscription.external_tenant_id,
                             "department_id": mapping.external_department_id or mapping.id}, config,
                            operation_key(db, subscription.id, "DEPT_REMOVE", target_id))
                        mapping.status = "deleted"; result_id = mapping.id
                        active_dept_maps.pop(target_id, None)
                ns["audit"](db, actor, f"saas_sync_{action}_{kind[:-1]}", "saas_sync", result_id,
                    f"SaaS {label}{'同步' if action == 'add' else '移除'}成功", category="delivery",
                    business_domain="saas", tenant_id=subscription.enterprise_id,
                    before=before,
                    after={"status": "active" if action == "add" else "deleted", "target_id": target_id,
                           "assignment": payload if kind == "users" and action == "add" else {}})
                db.commit()
                results.append({"id": target_id, "mapping_id": result_id, "status": "succeeded", "provider_result": provider_result})
            except Exception as exc:
                db.rollback()
                detail = str(exc.detail) if isinstance(exc, HTTPException) else "SaaS 同步操作失败"
                ns["audit"](db, actor, f"saas_sync_{action}_{kind[:-1]}", "saas_sync", target_id,
                    f"SaaS {label}{'同步' if action == 'add' else '移除'}失败：{detail}", category="delivery",
                    business_domain="saas", tenant_id=subscription.enterprise_id, risk_level="warning",
                    before=before, after={"status": "failed", "reason": detail}, result="failure")
                db.commit()
                results.append({"id": target_id, "status": "failed", "detail": detail})
        return {"items": results, "succeeded": sum(item["status"] == "succeeded" for item in results),
                "failed": sum(item["status"] == "failed" for item in results),
                "skipped": sum(item["status"] == "skipped" for item in results)}

    hooks = {"context": context}
    ns["saas_sync_management"] = hooks
    return hooks
