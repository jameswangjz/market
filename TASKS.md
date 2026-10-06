# 开发任务台账

状态：`TODO`、`IN_PROGRESS`、`REVIEW`、`BLOCKED`、`DONE`

| 编号 | 负责人 | 任务 | 依赖 | 状态 | 验收条件 |
|---|---|---|---|---|---|
| ARC-001 | 主 Agent | 需求、数据模型、接口边界和状态机固化 | 已确认需求 | DONE | 文档与代码契约一致 |
| ARC-002 | 主 Agent | 建立任务台账和集成门禁 | ARC-001 | DONE | 任务可追踪、接口可验证 |
| BE-001 | 后端 Agent | FastAPI、PG 模型、种子数据和认证 | ARC-001 | DONE | API 可启动，登录和租户可用 |
| BE-002 | 后端 Agent | 产品、审核和文件元数据 | BE-001 | DONE | 产品生命周期可运行 |
| BE-003 | 后端 Agent | 订单四域状态机 | BE-001 | DONE | 合法动作成功，非法动作拒绝 |
| BE-004 | 后端 Agent | 模拟支付、交付、售后 | BE-003 | DONE | 主流程可闭环 |
| BE-005 | 后端 Agent | 清算分账和审计 | BE-004 | DONE | 金额可复核、流水可追踪 |
| FE-001 | 前端 Agent | Vue 工作台、导航和登录 | ARC-001 | DONE | 可登录并加载首页 |
| FE-002 | 前端 Agent | 产品、订单、状态时间轴 | BE-001 | DONE | 页面与 API 联通 |
| FE-003 | 前端 Agent | 交付、清算、审计和用户页面 | BE-005 | DONE | 核心运营页面可用 |
| OPS-001 | 部署测试 Agent | PG、Redis、MinIO 和 K8S 资源 | ARC-001 | DONE | market 命名空间资源可部署 |
| OPS-002 | 部署测试 Agent | 镜像、NodePort 和健康检查 | FE-001,BE-001 | DONE | 前后端 Pod Ready |
| OPS-003 | 部署测试 Agent | 接口、状态机和端到端冒烟测试 | FE-002,BE-005 | DONE | P0 测试通过 |
| BE-006 | 后端 Agent | 手机号/邮箱注册与个人实名认证 | BE-001 | DONE | 注册、验证码、身份证材料和人工审核可闭环 |
| BE-007 | 后端 Agent | 企业实名认证、邀请入企和企业角色权限 | BE-006 | DONE | 企业人工审核、邀请、成员角色和订单权限可验证 |
| BE-008 | 后端 Agent | 平台角色、临时密码和通知平台配置 | BE-001 | DONE | 7 类平台角色可分配，短信/邮件配置可保存 |
| FE-004 | 前端 Agent | 注册、实名认证和系统设置工作台 | BE-006,BE-008 | DONE | 首页注册、认证材料、通知配置和平台角色页面可用 |
| BE-009 | 后端 Agent | 统一 API 网关控制面、鉴权、配额和调用统计 | BE-002,BE-007 | DONE | API 路由、API Key、限流、日配额和调用统计可用 |
| OPS-004 | 部署测试 Agent | 统一 API 网关集群和 API 服务独立部署 | BE-009,OPS-001 | DONE | market-gateway Pod Ready，API 请求可转发 |
| ARC-003 | 主 Agent | SaaS 需求、接口规范、计费与生命周期设计 | 已确认 SaaS 规则和参考源码 | DONE | 需求计划与实现边界文档完成 |
| BE-010 | 后端 Agent | SaaS 版本、OAuth2 适配、订阅和生命周期 | ARC-003 | DONE | 开通、续费、版本变更、关闭和恢复接口可用 |
| BE-011 | 后端 Agent | SaaS 用户/部门同步、幂等台账和升级支付联动 | BE-010 | DONE | 用户、部门、支付后 CHANGE 操作可追踪 |
| FE-005 | 前端 Agent | SaaS 接口规范下载入口 | BE-010 | DONE | SaaS 产品登记可下载接口文档 |
| OPS-005 | 部署测试 Agent | SaaS 版本部署、编译、健康检查和回归验证 | BE-010,BE-011,FE-005 | DONE | K8S Pod Ready，API 健康检查通过 |

## 集成门禁

1. 后端 `python -m compileall app` 和接口测试通过；
2. 前端 `npm run build` 通过；
3. 数据库初始化和种子数据成功；
4. Kubernetes 所有 P0 Pod Ready；
5. 登录、产品、订单、模拟支付、交付、售后、清算主流程通过；
6. 多租户越权访问被拒绝；
7. 未实现的外部接口没有被误暴露。
