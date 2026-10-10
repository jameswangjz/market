"""Explicit tenant selection and server-side trading eligibility."""
from fastapi import Depends, HTTPException
from decimal import Decimal
from sqlalchemy import select


def install(ns):
    app, User, Enterprise, Membership = (ns[name] for name in ("app", "User", "Enterprise", "Membership"))
    db_session, current_user = ns["db_session"], ns["current_user"]
    ProductReleaseVersion, FileObject, Order = (ns[name] for name in ("ProductReleaseVersion", "FileObject", "Order"))
    online = {"file", "api", "model_api", "tenant_access"}
    offline = {"training", "consulting", "custom"}

    def delivery_category(method):
        if method not in online | offline:
            raise HTTPException(400, "交付方式无效；对象存储交付已停止使用")
        return "online" if method in online else "offline"

    def sold(version):
        return select(Order.id).where(Order.product_version_id == version.id,
                                      Order.paid_amount > 0).limit(1)

    def protect_sold_file(db, version):
        if db.scalar(sold(version)):
            raise HTTPException(409, "已售版本的交付文件不可覆盖，请新增版本")

    def protect_sold_version(db, version, values):
        if not db.scalar(sold(version)):
            return
        for key, value in values.items():
            existing = getattr(version, key)
            if key in {"price", "cost"}:
                existing, value = Decimal(str(existing)), Decimal(str(value))
            if existing != value:
                raise HTTPException(409, "已售版本配置不可覆盖，请新增版本")

    def update_versions(db, product, versions):
        codes = [item["version_code"] for item in versions]
        if len(codes) != len(set(codes)):
            raise HTTPException(400, "产品版本号不能重复")
        existing = {version.version_code: version for version in product.versions}
        for version in existing.values():
            if version.version_code not in codes and db.scalar(sold(version)):
                raise HTTPException(409, "已售版本不可删除，请保留原版本")
        for item in versions:
            version = existing.get(item["version_code"])
            if version:
                protect_sold_version(db, version, item)
                for key, value in item.items():
                    setattr(version, key, value)
            else:
                product.versions.append(ProductReleaseVersion(**item))
        for version in list(product.versions):
            if version.version_code not in codes:
                if db.scalar(select(FileObject.id).where(FileObject.version_id == version.id).limit(1)):
                    version.status = "disabled"
                else:
                    product.versions.remove(version)

    def validate_submission(db, product):
        delivery_category(product.delivery_method)
        logo = db.get(FileObject, product.logo_file_id) if product.logo_file_id else None
        if not logo or logo.product_id != product.id or logo.file_role != "product_logo" or logo.status not in {"draft", "active"} or logo.scan_status != "clean":
            raise HTTPException(400, "请上传有效产品Logo并完成病毒扫描")
        versions = [version for version in product.versions if version.status == "active"]
        if not versions:
            raise HTTPException(400, "至少需要一个启用的产品版本")
        if any(version.price < version.cost for version in versions):
            raise HTTPException(400, "版本价格不能低于版本成本")
        if product.delivery_method == "file":
            for version in versions:
                file = db.scalar(select(FileObject).where(
                    FileObject.product_id == product.id, FileObject.version_id == version.id,
                    FileObject.file_role == "product_data", FileObject.status.in_(["draft", "active"]),
                    FileObject.scan_status == "clean"))
                if not file:
                    raise HTTPException(400, "每个启用版本须上传交付文件并完成病毒扫描")
        if product.delivery_method in {"api", "model_api"} and not product.upstream_url:
            raise HTTPException(400, "请配置API提供方后端地址")
        if product.delivery_method == "tenant_access" and (not product.application_url or not product.integration_api_url):
            raise HTTPException(400, "请配置SaaS应用访问地址及平台管理接口地址")

    def reason(user, enterprise, membership):
        if user.platform_role:
            return "平台角色账号不能作为企业购买方"
        if not user.is_active or user.activation_status != "active":
            return "用户尚未激活或已被禁用"
        if user.verified_status != "verified":
            return "请先完成个人实名认证"
        if not membership or membership.status != "active":
            return "用户不是该企业的有效成员"
        if membership.role not in {"super_admin", "enterprise_admin"}:
            return "仅企业超级管理员和企业管理员可订阅"
        if not enterprise or enterprise.verification_status != "verified":
            return "请先完成企业实名认证"
        return ""

    def require_buyer(db, user, enterprise_id):
        if not enterprise_id or not enterprise_id.strip():
            raise HTTPException(400, "必须明确选择购买企业")
        membership = db.scalar(select(Membership).where(
            Membership.user_id == user.id, Membership.enterprise_id == enterprise_id,
            Membership.status == "active"))
        enterprise = db.get(Enterprise, enterprise_id) if membership else None
        failure = reason(user, enterprise, membership)
        if failure:
            raise HTTPException(403, failure)
        return enterprise

    @app.get("/api/storefront/buyer-enterprises")
    def buyer_enterprises(user: User = Depends(current_user), db=Depends(db_session)):
        if user.platform_role:
            return {"items": []}
        memberships = db.scalars(select(Membership).where(
            Membership.user_id == user.id, Membership.status == "active"
        ).order_by(Membership.created_at)).all()
        items = []
        for member in memberships:
            enterprise = db.get(Enterprise, member.enterprise_id)
            if not enterprise:
                continue
            failure = reason(user, enterprise, member)
            items.append({"id": enterprise.id, "name": enterprise.name,
                          "role": member.role, "verification_status": enterprise.verification_status,
                          "eligible": not failure, "reason": failure})
        return {"items": items}

    return {"require_buyer": require_buyer, "delivery_category": delivery_category,
            "validate_submission": validate_submission, "update_versions": update_versions,
            "protect_sold_version": protect_sold_version, "protect_sold_file": protect_sold_file}
