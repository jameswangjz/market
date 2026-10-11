"""Scoped task tracking for product registration and integration UI acceptance."""
import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("start", "finish"))
    parser.add_argument("--output")
    parser.add_argument("--runtime-tag", default="")
    parser.add_argument("--browser-evidence")
    args = parser.parse_args()
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        headers = {"Authorization": "Bearer " + m.issue_token(admin)}
    updates = {
        "TRD-FE-004": ("in_progress", 30, "补齐登记弹框的线上线下分组、每版本接入字段和附件绑定；依据真实接口契约实施，不伪造未实现的OIDC配置。"),
        "TRD-FE-005": ("in_progress", 35, "开发各版本接入验证状态/结果与提供方凭据下载入口；修复pending_integration状态下凭据下载被拒问题，执行权限隔离回归。"),
        "TRD-QA-004": ("in_progress", 55, "咨询完整履约链路已在真实PG/MinIO/ClamAV复测通过；正在扩展培训/定制开发和拒绝/低于成本分支。"),
    }
    if args.phase == "finish":
        updates = {
            "TRD-FE-004": ("in_progress", 80, "已实现线上线下分组、Logo必填、每版本文件/API接入配置、千次配额和附件重试绑定；80项前端测试及316项后端回归通过。兼容性、OIDC回调和前端逐版本已售禁用标志仍待后端契约，不标全部完成。"),
            "TRD-FE-005": ("in_progress", 85, "API/模型API详情已有每版本接入状态、健康结果、验证入口和提供方规范/凭据下载；修复pending_integration下载权限和实际会话角色显示，多企业/成员/平台遗留成员关系权限测试通过。SaaS接入与OIDC依赖仍未完成。"),
            "TRD-QA-004": ("in_progress", 85, "培训/咨询/定制开发真实PG/MinIO/ClamAV顺序回归通过：拒绝、改价成本、支付、开工、整改重提、验收及附件审计；两项三交付回滚专项通过。并发支付确认与报价支付交叉实测仍未完成，模拟人工付款不作为真实支付验收。"),
        }
    with TestClient(m.app) as client:
        for code, (status, progress, note) in updates.items():
            response = client.patch("/api/development/tasks/" + code, headers=headers,
                                    json={"status": status, "progress": progress, "note": note})
            assert response.status_code == 200, (code, response.text)
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    if args.phase == "finish":
        snapshot["verification"] = {
            "runtime_image_tag": args.runtime_tag, "backend_tests": 316, "frontend_tests": 80,
            "offline_live_methods": ["training", "consulting", "custom"],
            "limits": ["Compatibility/OIDC callback/sold-state UI contract pending",
                       "SaaS provisioning and SSO not accepted by this batch",
                       "No concurrent payment or quote/payment cross-race evidence",
                       "Payment remains simulated/manual, no live channel"],
        }
        if args.browser_evidence:
            evidence = json.loads(Path(args.browser_evidence).read_text(encoding="utf-8-sig"))
            assert evidence["status"] == "passed" and evidence["mode"] == "read-only"
            snapshot["verification"]["browser"] = {
                "status": evidence["status"], "mode": evidence["mode"],
                "viewports": [sample["viewport"] for sample in evidence["samples"]],
                "screenshots": len(evidence["screenshots"]),
                "deployment_tag": evidence["deploymentTag"],
                "limit": "No browser uploads/save or successful verification POST; credential headers only",
            }
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": args.phase, "counts": snapshot["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
