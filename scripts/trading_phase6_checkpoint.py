"""Scoped live task updates for quota cutover and subscription UI acceptance."""
import argparse
from collections import Counter
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
    args = parser.parse_args()
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        headers = {"Authorization": "Bearer " + m.issue_token(admin)}
    started = {
        "TRD-QA-002": ("in_progress", 30, "正在复核月锚点、真实Redis配额和开发环境APISIX联调；已完成测试不重复重置。"),
        "TRD-BE-011": ("in_progress", 88, "新增原子旧计数切换保护及显式保守迁移工具，19项真实Redis/Lua专项通过；等待开发环境真实网关迁移复测。"),
        "TRD-FE-009": ("in_progress", 50, "浏览器发现并修复会话缺少企业认证状态导致续费按钮不可见的问题，订单所属企业用于续费权限；正在部署并重跑桌面/移动验收。升降级部分仍待后续后端。"),
    }
    finished = {
        "TRD-QA-002": ("done", 100, "307项后端回归、14项真实Lua/Redis配额测试、19项迁移专项及开发环境33项三副本原生APISIX检查通过。月末/闰年/1-36月、当前未来段、北京时间日界、订阅月、0不限、并发共享计数、续费不清零覆盖；真实PG并发复测通过。"),
        "TRD-BE-011": ("in_progress", 92, "原子切换保护和显式预览/确认/保守用量迁移已实现，真实APISIX旧计数2次迁移后月计数20->22，重复回执不再计入。旧累计水位会保守计入当前日/月，绝不清零。精确旧周期重建、跨历史版本迁移、Redis Cluster及大规模停顿评估仍未完成。"),
        "TRD-FE-009": ("in_progress", 55, "真实隔离PG夹具的桌面1440/移动390只读验收通过，续费按钮、1-36月弹框、刷新/错误恢复及布局已验证；无真实写请求或原生弹框。会话认证状态和多企业订单上下文已修复。升降级报价、兼容确认及支付/退款资源进度仍依赖BE016/017/018。"),
    }
    client = TestClient(m.app)
    try:
        for code, (status, progress, note) in (started if args.phase == "start" else finished).items():
            response = client.patch("/api/development/tasks/" + code, headers=headers,
                                    json={"status": status, "progress": progress, "note": note})
            assert response.status_code == 200, (code, response.text)
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    finally:
        client.close()
    if args.phase == "finish":
        snapshot["verification"] = {
            "backend_tests": 307, "real_quota_tests": 14, "real_cutover_tests": 19,
            "native_apisix_checks": 33, "frontend_tests": 68,
            "runtime_image_tag": args.runtime_tag,
            "browser": {"mode": "read-only", "viewports": [1440, 390], "status": "passed"},
            "trd_counts": dict(Counter(row["status"] for row in snapshot["items"] if row["code"].startswith("TRD-"))),
            "limits": ["Conservative lifetime watermark, not exact historical cycle reconstruction",
                       "Mixed historical versions and Redis Cluster migration not accepted",
                       "Browser has no real renewal POST; HTTP and PG tests cover writes",
                       "Version-change, SaaS/OIDC and real payment remain separate unfinished work"],
        }
    if args.output:
        Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": args.phase, "counts": snapshot["counts"],
                      "verification": snapshot.get("verification")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
