# 开发任务台账

状态：`TODO`、`IN_PROGRESS`、`REVIEW`、`BLOCKED`、`DONE`

| 编号 | 负责人 | 任务 | 依赖 | 状态 | 验收条件 |
|---|---|---|---|---|---|
| ARC-001 | 主 Agent | 需求、数据模型、接口边界和状态机固化 | 已确认需求 | DONE | 文档与代码契约一致 |
| ARC-002 | 主 Agent | 建立任务台账和集成门禁 | ARC-001 | DONE | 任务可追踪、接口可验证 |
| BE-001 | 后端 Agent | FastAPI、PG 模型、种子数据和认证 | ARC-001 | DONE | API 可启动，登录和租户可用 |
| BE-002 | 后端 Agent | 产品、审核和文件元数据 | BE-001 | IN_PROGRESS | 产品生命周期可运行 |
| BE-003 | 后端 Agent | 订单四域状态机 | BE-001 | DONE | 合法动作成功，非法动作拒绝 |
| BE-004 | 后端 Agent | 模拟支付、交付、售后 | BE-003 | IN_PROGRESS | 主流程可闭环 |
| BE-005 | 后端 Agent | 清算分账和审计 | BE-004 | IN_PROGRESS | 金额可复核、流水可追踪 |
| FE-001 | 前端 Agent | Vue 工作台、导航和登录 | ARC-001 | DONE | 可登录并加载首页 |
| FE-002 | 前端 Agent | 产品、订单、状态时间轴 | BE-001 | IN_PROGRESS | 页面与 API 联通 |
| FE-003 | 前端 Agent | 交付、清算、审计和用户页面 | BE-005 | TODO | 核心运营页面可用 |
| OPS-001 | 部署测试 Agent | PG、Redis、MinIO 和 K8S 资源 | ARC-001 | DONE | market 命名空间资源可部署 |
| OPS-002 | 部署测试 Agent | 镜像、NodePort 和健康检查 | FE-001,BE-001 | DONE | 前后端 Pod Ready |
| OPS-003 | 部署测试 Agent | 接口、状态机和端到端冒烟测试 | FE-002,BE-005 | IN_PROGRESS | P0 测试通过 |

## 集成门禁

1. 后端 `python -m compileall app` 和接口测试通过；
2. 前端 `npm run build` 通过；
3. 数据库初始化和种子数据成功；
4. Kubernetes 所有 P0 Pod Ready；
5. 登录、产品、订单、模拟支付、交付、售后、清算主流程通过；
6. 多租户越权访问被拒绝；
7. 未实现的外部接口没有被误暴露。
