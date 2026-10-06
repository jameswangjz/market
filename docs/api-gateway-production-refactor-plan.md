# API Gateway 生产环境重构开发文档

版本：v1.0  
适用范围：数据集运营服务管理平台的 API 服务、模型 API 和后续 SaaS 资源服务  
文档状态：开发基线

## 1. 建设目标

将当前单副本 FastAPI 代理网关重构为生产可用的“运营平台控制面 + Apache APISIX 数据面”架构，满足以下目标：

- API、模型 API 统一通过 APISIX 网关集群对外提供服务；
- 产品审核、路由发布、OAuth 配置、版本限流和订单授权由运营平台统一管理；
- 网关数据面支持多副本、负载均衡、健康检查、故障摘除和滚动升级；
- 限流、配额、认证、调用统计、审计和告警具备可观测性；
- 现有订单、API Key、统一 OAuth2 和产品版本规则保持兼容；
- 提供运营平台内嵌的网关管理端，不要求运营人员直接操作 APISIX Admin API。

## 2. 当前实现和主要问题

当前 `gateway/app/main.py` 是基于 FastAPI、Uvicorn 和 httpx 的自研反向代理服务，使用 PostgreSQL 保存路由和调用记录，使用 Redis 保存限流计数。当前 Kubernetes Deployment 为单副本，OAuth Token 缓存在单个进程内存中。

主要问题：

1. 网关数据面和业务控制面耦合，路由转发、认证、配额和业务查询均由 Python 请求处理链完成；
2. 单副本无法满足节点故障自动接管和滚动升级期间的连续服务；
3. 进程内 OAuth Token 缓存无法在多个副本之间共享；
4. 当前 Kubernetes Service 只提供 NodePort，缺少正式入口、负载均衡和 TLS 终止方案；
5. 网关配置、插件策略和业务产品配置尚未形成统一的发布版本和回滚机制；
6. 后端虽有网关统计接口，但运营平台缺少配额、限流、健康状态和调用趋势的完整管理页面；
7. 监控、日志、告警和故障演练尚未形成生产闭环。

## 3. 目标架构

```text
平台用户/企业应用
        |
        | HTTPS / LoadBalancer / Ingress
        v
APISIX Data Plane 集群（2～3 副本或以上）
        |
        | 路由、认证、限流、配额、超时、健康检查、统计
        v
第三方 API / 模型 API / SaaS 资源服务

运营平台前端
        |
        v
FastAPI 控制面
  - 产品/版本/订单/凭据/授权
  - 网关配置编排
  - 发布、回滚、健康检查
  - APISIX Admin API 适配
        |
        +--> PostgreSQL：业务数据、配置快照、发布记录、审计
        +--> Redis：共享 Token 缓存、配额计数、短期状态
        +--> APISIX Admin API / APISIX Ingress Controller
```

### 3.1 组件职责

| 组件 | 职责 |
|---|---|
| 运营平台 FastAPI | 业务权限、产品审核、订单授权、凭据生命周期、配置编排和发布审计 |
| Apache APISIX | 生产数据面转发、认证插件、限流、配额、超时、健康检查和负载均衡 |
| APISIX Admin API 或声明式配置 | 接收经过业务校验的路由和策略配置 |
| PostgreSQL | 保存平台配置源数据、发布版本、调用汇总和审计信息 |
| Redis | 共享 OAuth Token 缓存、分布式限流/配额计数和短期锁 |
| Kubernetes | 多副本调度、Service 负载均衡、探针、滚动升级和故障重建 |
| Prometheus/Grafana/日志系统 | 指标、趋势、告警、网关访问日志和故障定位 |

FastAPI 保留为业务控制面，不再作为生产请求的主要转发数据面。现有自研网关保留为开发兼容模式和迁移期间的回退方案。

## 4. 功能需求拆解

### 4.1 网关基础能力

- APISIX 以 Kubernetes Deployment 方式部署，默认至少 3 个数据面副本；
- 通过 Service 和正式入口暴露 HTTPS，NodePort 仅用于开发和验收环境；
- 配置 readiness/liveness 探针、PodDisruptionBudget、滚动升级和节点反亲和；
- 支持 API、模型 API 独立路由，路由键、版本和后端地址可配置；
- 支持 HTTP/HTTPS、JSON、文件响应和流式响应；
- 支持后端连接超时、响应超时、健康检查和不可用后端摘除；
- 支持按权重将请求转发至多个上游实例。

### 4.2 认证和授权

- 调用方继续支持订单级 API Key 和 Bearer 方式；
- API Key 必须关联企业、订单、产品版本和有效期；
- 订单取消、退款、关闭、过期或凭据停用后，网关即时拒绝访问；
- 网关到 API/模型提供方继续使用平台统一 OAuth2 `client_credentials`；
- OAuth Token 缓存必须改为 Redis 共享缓存，缓存键按产品和作用域隔离；
- 提供方收到的 Token 必须包含并校验 issuer、audience、product_id、client_id 和 scope；
- 调用企业身份由网关注入，提供方不得信任调用方自行传入的企业身份请求头；
- Admin API 只能由控制面服务账号访问，不得暴露给公网或普通业务用户。

### 4.3 版本、限流和配额

- 产品每个版本独立保存价格、每分钟限流、每日配额和每月配额；
- 订单凭据继承下单版本的策略；
- 允许企业管理员在授权范围内查看凭据策略，但不得绕过平台配置直接修改 APISIX；
- 限流和配额使用 Redis 或 APISIX 共享策略，不能依赖单 Pod 内存；
- 超限统一返回 `429`，响应中提供 request id 和错误码；
- 版本升级、降级、退款、订单取消后，凭据对应策略必须同步更新或回收；
- 配置发布前校验版本、路由、策略和订单授权的一致性。

### 4.4 路由发布和回滚

1. 提供方提交或平台管理员录入后端地址、路由、版本、健康检查和策略建议；
2. FastAPI 控制面保存草稿并生成配置校验结果；
3. 产品审核通过后执行后端健康检查；
4. 健康检查通过后生成 APISIX 路由、上游和插件配置；
5. APISIX 配置成功后，平台将发布版本标记为 `active`；
6. 任一阶段失败，发布版本标记为 `failed`，不影响上一稳定版本；
7. 支持按发布记录回滚到上一稳定配置；
8. 所有配置变更记录操作者、时间、请求摘要、APISIX 响应和回滚结果。

### 4.5 WEB 管理端

在现有运营平台中增加“API 网关管理”视图，至少包括：

- 路由列表：产品、版本、路由键、后端地址、发布状态、健康状态；
- 版本策略：每分钟限流、每日配额、每月配额、超时和更新时间；
- 凭据管理：企业、订单、版本、状态、有效期、停用和重新生成；
- 发布操作：健康检查、发布、重新发布、回滚；
- 调用统计：调用总量、成功率、错误数、平均耗时、状态码、请求路径和时间趋势；
- 资源状态：APISIX 副本数、Ready 状态、上游健康状态和最近告警；
- 操作审计：配置修改、发布、回滚、凭据操作和权限拒绝记录。

产品提供方只查看和管理自己产品授权范围内的配置；平台超级管理员和平台运营人员可以管理全平台路由；企业普通用户只能查看本人有权使用的订单凭据。

### 4.6 监控和告警

至少采集：

- 请求总量、成功率、4xx/5xx 数量；
- 429 限流次数、配额耗尽次数；
- P50/P95/P99 延迟；
- 上游超时、连接失败、健康检查失败；
- APISIX Pod CPU、内存、重启次数和 Ready 状态；
- OAuth Token 获取失败、缓存命中率和刷新失败；
- Redis、PostgreSQL、etcd/APISIX 配置服务连接状态。

首期告警规则：网关副本不足、连续 5 分钟错误率超阈值、上游连续健康检查失败、Token 获取连续失败、Redis 不可用、配额计数异常和 APISIX Admin API 发布失败。

## 5. 数据模型和接口边界

### 5.1 新增或调整数据

- `gateway_config_revisions`：路由配置发布版本、配置摘要、状态、操作者和回滚来源；
- `gateway_publish_records`：发布开始/结束时间、目标 APISIX、结果、错误信息和审计关联；
- `gateway_upstreams`：上游地址、权重、健康检查、超时和 TLS 配置；
- `gateway_policy_bindings`：产品版本与限流、配额、认证策略的绑定；
- `gateway_metric_snapshots`：按时间窗口保存调用汇总，避免每次从明细表聚合；
- `gateway_alert_events`：告警规则、事件、确认人、恢复时间和处理记录。

保留现有 `api_gateway_routes`、`api_credentials`、`api_usage` 表，通过迁移脚本增加发布版本和 APISIX 资源标识字段，不直接删除历史数据。

### 5.2 控制面接口

建议新增或调整：

```text
GET  /api/gateway/routes
GET  /api/products/{product_id}/gateway-config
PUT  /api/products/{product_id}/gateway-config
POST /api/products/{product_id}/gateway-config/validate
POST /api/products/{product_id}/gateway-config/publish
POST /api/products/{product_id}/gateway-config/rollback
GET  /api/products/{product_id}/gateway-revisions
GET  /api/products/{product_id}/gateway-usage
GET  /api/products/{product_id}/gateway-health
GET  /api/gateway/overview
GET  /api/gateway/alerts
```

控制面接口负责权限和业务一致性；APISIX Admin API 只允许由后端适配器调用。前端不直接携带 APISIX Admin Key。

### 5.3 统一错误和幂等

- 发布和回滚请求必须支持 `Idempotency-Key`；
- 每次配置变更生成唯一 revision；
- 返回统一 `code`、`message`、`request_id`；
- APISIX 发布超时后必须查询实际状态，禁止简单重复创建导致重复路由；
- 配置发布失败必须保留失败原因，便于控制台显示和重试。

## 6. 非功能需求

| 项目 | 目标 |
|---|---|
| 可用性 | 网关数据面单 Pod 故障时业务自动切换到其他副本 |
| 扩展性 | 数据面副本可独立扩容，控制面与数据面解耦 |
| 性能 | 正常网关转发不依赖每次请求访问 PostgreSQL |
| 安全 | Admin API 内网可达、密钥使用 K8S Secret、生产使用 HTTPS |
| 一致性 | 产品审核、订单授权、凭据状态和网关策略最终一致且可追踪 |
| 可恢复性 | 配置可回滚，故障恢复后可重新同步稳定版本 |
| 可观测性 | 请求、配置、发布、故障、告警和审计均可查询 |

## 7. 分阶段开发计划

### 阶段一：架构和基础设施

完成 APISIX、etcd、Redis 高可用基础资源，定义控制面与数据面边界、Secret、Service、Ingress、探针和发布配置。

### 阶段二：控制面适配

完成 APISIX Admin API 适配器、路由/上游/插件配置编排、配置校验、发布状态、发布记录和回滚机制。

### 阶段三：认证、限流和授权迁移

迁移 API Key、订单授权、版本策略、OAuth Token 缓存和企业身份注入，完成旧 FastAPI 网关兼容和切换开关。

### 阶段四：管理端和统计

在现有运营平台增加网关管理页面、配置发布、凭据策略、调用统计、健康状态、告警和审计展示。

### 阶段五：测试、压测和切换

完成接口回归、并发压测、Pod 故障、Redis 故障、上游故障、发布失败、回滚和数据一致性演练，再将生产流量切换到 APISIX。

## 8. 验收标准

1. APISIX 至少 3 个副本，任意一个副本删除后请求仍可成功；
2. 网关入口经过 Kubernetes Service/Ingress 负载均衡，生产不直接暴露 Admin API；
3. API 和模型 API 可完成审核、健康检查、自动发布、调用、停用和回滚；
4. API Key、OAuth2、订单状态和产品版本策略全链路生效；
5. 每分钟、每日、每月限流/配额在多个网关副本下结果一致；
6. 提供方 OAuth Token 在多个副本间共享缓存，Token 服务异常时有明确错误和告警；
7. WEB 管理端可查看路由、版本策略、凭据、健康状态、调用统计和发布历史；
8. 上游超时、5xx、429、无效凭据和订单失效均返回统一错误格式；
9. 发布失败不覆盖上一稳定版本，回滚后旧版本恢复服务；
10. 监控可发现网关副本、Redis、数据库、上游服务和 OAuth Token 的异常；
11. 完成至少一次网关 Pod 故障、Redis 故障、上游不可用和配置回滚演练；
12. 前端构建、后端测试、Kubernetes 部署和端到端验收全部通过。

## 9. 风险和决策

- APISIX Admin API 的权限必须限制在控制面服务账号，不能让业务前端直接调用；
- APISIX、etcd、Redis 和监控系统本身也是生产依赖，需要纳入容量、备份和升级计划；
- 现有 API Key 哈希和订单授权数据必须保留，迁移期间不能直接重新生成全部凭据；
- 生产切换采用灰度路由或按产品逐个切换，保留 FastAPI 网关回退入口；
- 第一阶段优先建设自托管开源 APISIX 方案，暂不引入 Kong Enterprise 或商业控制台依赖。

## 10. 当前实现进度

已完成第一轮实际改造：

- `market-etcd` 已部署到 `market` 命名空间；
- APISIX 3.19 数据面已部署为 2 个 Kubernetes 副本；
- APISIX 公网测试入口为 NodePort `30082`，Admin API 仅通过 ClusterIP 提供给控制面；
- FastAPI 已接入 APISIX Admin API，可执行配置校验、路由发布、发布记录查询和回滚；
- 已使用“制造过程行业模型”完成保存配置、后端健康检查、APISIX 发布、API Key 调用和限流响应头验证；
- 当前采用兼容模式，APISIX 将请求转发至现有 `market-gateway`，因此旧的订单授权和 API Key 逻辑仍然有效；
- API Key 原生 APISIX Consumer 同步、Redis 共享 OAuth Token、生产入口 TLS、监控告警和运营平台管理页面仍待后续任务完成。

第二轮已开始：

- `BE-014`：上游 OAuth Token 已改为 Redis 共享缓存，正在进行多副本和故障场景验证；
- `BE-015`：APISIX `limit-count` 已使用 Redis 策略，按 `X-API-Key` 请求头分桶，版本限流值随发布配置下发；日/月配额暂由兼容控制链继续校验；
- `OPS-007`：APISIX Prometheus 指标已在 `market-apisix-metrics:9091` 暴露，监控采集和告警规则尚待接入。
