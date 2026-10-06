# API 应用接入统一网关接口与规范

版本：v1.1
适用范围：数据集运营服务管理平台中的“API 服务”和“模型 API”产品

## 1. 总体架构

平台采用“统一 API 网关集群 + API 服务独立部署”架构。API 服务提供方独立部署业务 API 服务，平台统一 API 网关负责入口路由、身份认证、企业授权、限流、配额、超时控制和调用统计。

```text
API 调用方
    |
    | X-API-Key / Authorization: Bearer
    v
统一 API 网关集群（market-gateway）
    |
    | 按 route_key 路由
    +--> API 服务 A 独立 Service/Deployment
    +--> API 服务 B 独立 Service/Deployment
    +--> API 服务 C 独立 Service/Deployment
```

网关地址：`http://<平台地址>:30081`。生产环境应通过 HTTPS 域名或 Ingress 暴露，不应直接使用 NodePort。

API 提供方的业务 API 不要求部署在数据集运营服务管理平台所在的 Kubernetes 集群中。`upstream_url` 可以是第三方公网 HTTPS 地址、专用网络地址，或平台 Kubernetes 集群内的 Service 地址；前提是统一 API 网关所在网络能够访问该地址，并完成 TLS、访问控制和健康检查配置。

## 2. API 产品上架流程

1. 在“登记数据或服务”中选择产品类型“API 服务”。
2. 完成产品名称、所属目录、提供方、描述、适用场景、版本、价格策略、安全等级和授权条件等元数据登记。
3. 提供方准备独立部署的 API 服务，并提供网关可访问的后端地址。
4. 平台管理员或产品提供企业管理员配置网关路由。
5. 产品通过平台审核并发布。
6. 产品发布并启用网关路由；购买方完成支付后，在订单详情页生成 API Key。
7. 调用方按照本规范访问统一 API 网关。
8. 订单取消、退款、关闭或到期后，订单凭据不能继续通过网关鉴权；重新生成凭据需要重新满足订单授权条件。

## 3. 网关配置接口

所有平台管理接口基础路径为 `/api`，需要平台登录令牌。

### 3.1 保存网关配置

```http
PUT /api/products/{product_id}/gateway-config
Authorization: Bearer <platform-token>
Content-Type: application/json
```

请求示例：

```json
{
  "upstream_url": "http://api-quality-service:8080/v1",
  "route_key": "quality-api",
  "version": "v1",
  "auth_mode": "api_key",
  "rate_limit_per_minute": 600,
  "daily_quota": 100000,
  "monthly_quota": 3000000,
  "timeout_ms": 30000,
  "strip_prefix": true
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `upstream_url` | 是 | API 服务在 Kubernetes 或内部网络中的 HTTP/HTTPS 地址 |
| `route_key` | 否 | 网关路由标识；为空时由平台生成 |
| `version` | 否 | API 版本，例如 `v1`、`v2` |
| `auth_mode` | 否 | 首版支持 `api_key`；内部健康检查场景可配置 `none` |
| `rate_limit_per_minute` | 否 | 单个 API Key 每分钟最大请求数 |
| `daily_quota` | 否 | 单个 API Key 每日最大请求数 |
| `monthly_quota` | 否 | 单个 API Key 每月最大请求数；`0` 表示不单独限制，使用每日配额 |
| `timeout_ms` | 否 | 网关等待后端响应的最大时间，范围 100 至 120000 毫秒 |
| `strip_prefix` | 否 | 是否只将路由后的路径转发给后端服务 |

### 3.2 发布网关路由

```http
POST /api/products/{product_id}/gateway-config/publish
Authorization: Bearer <platform-token>
```

产品必须已经完成产品审核并处于“已发布”状态，网关路由才可以启用。

### 3.3 生成 API Key

```http
POST /api/products/{product_id}/gateway-credentials
Authorization: Bearer <enterprise-admin-token>
Content-Type: application/json
```

```json
{
  "name": "质量分析系统生产凭证",
  "enterprise_id": "可选，平台管理员为指定企业生成",
  "rate_limit_per_minute": 300,
  "daily_quota": 50000,
  "monthly_quota": 1500000,
  "expires_at": "2027-12-31T23:59:59Z"
}
```

产品提供方可以配置路由、后端地址和产品默认策略，但不能取得购买方 API Key 原文。API 调用方凭据由购买方在已支付订单页面生成，API Key 只在生成或重新生成响应中返回一次。平台数据库只保存不可逆哈希，不保存密钥原文。

### 3.4 订单页面管理 API 凭据

购买订单的调用方可以在已支付订单的订单详情页管理该订单对应的 API 凭据，不需要调用后台管理接口。支持已完成个人实名认证的订单所有者，以及已完成企业实名认证的企业超级管理员；企业管理员权限也可以保留用于企业内部运维。接口会校验当前用户与订单所有者或购买企业的关系。

查询凭据状态：

```http
GET /api/orders/{order_id}/api-credentials
Authorization: Bearer <platform-token>
```

生成或获取凭据：

```http
POST /api/orders/{order_id}/api-credentials
Authorization: Bearer <platform-token>
Content-Type: application/json

{"name":"质量分析生产凭据"}
```

成功响应中返回 `api_key` 原文、`route_key` 和网关路径。`api_key` 只在本次生成响应中返回，平台不会从数据库恢复已生成的密钥原文。

停用凭据：

```http
POST /api/orders/{order_id}/api-credentials/{credential_id}/revoke
Authorization: Bearer <platform-token>
```

重新生成凭据：

```http
POST /api/orders/{order_id}/api-credentials/{credential_id}/regenerate
Authorization: Bearer <platform-token>
```

重新生成会立即停用旧凭据，并在响应中返回新的 `api_key`。API 调用方应在生成或重新生成后立即保存密钥，并在客户端配置新的凭据。订单未支付、API 路由未启用或凭据已停用时，网关不会允许调用。

## 4. API 服务端接入规范

API 服务应提供标准 HTTP 接口，推荐使用 JSON 作为请求和响应格式。

### 4.1 请求要求

- 使用 HTTP/HTTPS。
- 正确处理 `GET`、`POST`、`PUT`、`PATCH`、`DELETE` 等业务方法。
- 支持标准 `Content-Type: application/json`。
- 请求体不得依赖网关改写后的认证信息。
- 单次请求应在网关配置的超时时间内完成。
- 不应依赖客户端直接传入企业身份，企业身份以网关注入请求头为准。

### 4.2 网关注入请求头

认证成功后，网关会向后端 API 服务注入：

```http
X-Market-Enterprise-Id: <调用企业ID>
X-Market-Route-Key: <网关路由标识>
```

API 服务可以基于 `X-Market-Enterprise-Id` 进行企业级数据权限判断。后端服务不得信任客户端自行伪造的同名请求头；生产环境应只允许来自网关服务网段的访问。

### 4.3 响应要求

推荐返回：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "request_id": "业务请求号"
}
```

HTTP 状态码应符合以下约定：

| 状态码 | 含义 |
|---|---|
| 200 | 请求成功 |
| 400 | 请求参数错误 |
| 401 | 认证失败或凭证缺失 |
| 403 | 无权访问资源 |
| 404 | API 资源不存在 |
| 409 | 请求状态冲突或幂等冲突 |
| 429 | 超过限流或配额 |
| 500 | API 服务内部错误 |
| 502 | 网关无法连接后端服务 |
| 504 | 后端服务响应超时 |

## 5. 网关调用规范

### 5.1 调用地址

```http
<gateway-base-url>/gateway/{route_key}/{api-path}
```

例如：

```http
GET http://192.168.10.10:30081/gateway/quality-api/v1/quality-score?equipment_id=EQ-001
X-API-Key: mk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Accept: application/json
```

也可以使用：

```http
Authorization: Bearer mk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 5.2 限流与配额

- Redis 保存分布式分钟计数和日计数。
- API Key 可以使用自身限额覆盖产品默认限额。
- 超过分钟限流或每日配额时，网关返回 `429`，不会转发到后端服务。
- 配置了 `monthly_quota` 时，网关按 UTC 月份累计调用量；超过月度配额同样返回 `429`。
- 调用方应采用指数退避，不应持续重试造成流量放大。
- 生产调用应设置业务幂等号，例如 `X-Request-Id` 或 `Idempotency-Key`。

### 5.3 参数、幂等和内容类型

- 路径参数和查询参数按产品 API 定义传递，网关不会修改业务参数。
- `Content-Type`、`Accept`、`Content-Encoding` 等非认证请求头会按原值转发；认证头不会转发给后端。
- `X-Request-Id` 由调用方生成并在网关、第三方服务和日志中贯穿；写操作建议同时使用 `Idempotency-Key`。
- 第三方服务应保证相同幂等号的重复写请求不会重复扣费、重复创建或重复变更。

### 5.4 文件、大响应和流式响应

- 文件下载接口应返回正确的 `Content-Type`、`Content-Disposition` 和文件名，网关透传响应头和二进制内容，不应将文件内容转换为 JSON。
- 大响应建议使用分页、压缩或异步任务；第三方服务应在登记资料中说明最大响应大小和超时要求。
- SSE 或其他流式接口调用时增加 `X-Market-Stream: true`，或将 `Accept` 设置为 `text/event-stream`。网关以流式方式转发数据，不等待完整响应；第三方服务应定期发送心跳并处理客户端断开。
- 流式响应仍受网关超时和 API Key 配额限制，调用方应保存 `X-Request-Id` 以便追踪。

### 5.5 错误、重试和授权回收

网关和第三方服务建议统一返回：

```json
{"code":"QUOTA_EXCEEDED","message":"超过 API 每日调用配额","request_id":"req-001","retryable":false}
```

调用方处理规则：

| 错误 | 是否重试 | 处理方式 |
|---|---|---|
| 401 | 否 | 检查 API Key 是否停用、过期或配置错误 |
| 403 | 否 | 检查订单授权、企业权限和业务数据权限 |
| 404 | 否 | 检查路由、版本和资源路径 |
| 409 | 按业务决定 | 使用相同幂等号查询最终结果 |
| 429 | 是 | 按 `Retry-After` 或指数退避后重试 |
| 502/504 | 是 | 指数退避，最多重试 3 次，并使用同一幂等号 |
| 5xx | 是 | 仅对幂等请求重试，非幂等请求先查询结果 |

统一 API 网关当前不自动重试业务请求，避免非幂等操作重复执行。订单取消、退款、关闭、授权到期或凭据停用后，网关每次请求都会重新校验订单授权并拒绝调用。

## 6. Kubernetes 部署示例（可选）

API 提供方可以自行选择虚拟机、物理服务器、云主机或其他 Kubernetes 集群部署 API。以下仅是 API 服务部署在平台或可达 Kubernetes 环境中的示例，不构成强制部署要求。

每个 API 应用独立部署为自己的 Deployment 和 Service，网关只配置 Service 地址。例如：

```yaml
apiVersion: v1
kind: Service
metadata:
  name: quality-api-service
  namespace: market
spec:
  selector:
    app: quality-api
  ports:
    - port: 8080
      targetPort: 8080
```

网关配置的后端地址可以是：

```text
http://quality-api-service.market.svc.cluster.local:8080/v1
```

API 服务应配置 Readiness、Liveness、资源请求与限制，并支持至少两个副本。不同 API 服务之间不得共享不可控的本地状态；会话、任务和缓存应使用平台认可的持久化组件。

## 7. 安全要求

- 生产环境必须使用 HTTPS。
- API Key 不得写入代码、镜像或日志。
- API Key 只显示一次，泄露后应立即停用并重新生成。
- API 服务只允许网关网络访问，避免绕过网关直接暴露。
- 敏感参数不得出现在普通日志中。
- API 服务应自行完成业务级数据权限、参数校验和敏感数据脱敏。
- 涉及国密要求的 API，应在产品登记和安全合规审核中明确 SM2/SM3/SM4 使用边界。

## 8. 版本、变更和下线

- 每个 API 产品应使用稳定的 `route_key`，业务版本通过 `/v1`、`/v2` 等路径区分。
- 新版本上线时应先创建新版本路由并完成联调，再通知调用方切换；旧版本至少保留一个双方约定的兼容期。
- 版本价格、订单版本和配额策略以平台订单记录为准；旧订单不会自动获得新版本权限。
- 路由地址、认证方式、响应结构等不兼容变更必须提前通知，并提供迁移说明。
- API 下线前应停止新订单和新凭据生成，保留已购买版本至约定到期时间；紧急安全下线应记录审计信息并通知调用方。

## 9. 测试环境与联调验收

开发环境网关地址：`http://192.168.10.10:30081`。生产环境必须使用 HTTPS 域名或 Ingress。

联调步骤：

1. API 提供方在平台登记 API 产品，提供可访问的测试后端地址，配置路由和限流策略。
2. 产品审核发布并启用路由。
3. 测试购买方完成模拟支付，在订单详情页生成测试 API Key；平台不提供固定公共密钥。
4. 使用网关健康检查 `GET /health`、业务正常请求、无效凭据、停用凭据、过期凭据、超额、后端 502 和超时 504 场景进行验证。
5. 在平台调用统计中核对调用次数、成功率、HTTP 状态码、平均耗时和请求/响应字节数。

最低验收标准：

- 未携带或错误 API Key 返回 401；停用、过期或订单授权失效返回 401/403。
- 有效凭据可以访问正确路由，企业身份请求头能够传递到后端。
- 分钟、每日和月度配额分别生效，超额请求返回 429 且不转发到后端。
- 文件内容、响应头和流式数据能够正常透传。
- 后端不可用返回 502，后端超时返回 504，调用统计仍保留失败记录。
- 相同 `X-Request-Id` 或 `Idempotency-Key` 的写请求不会造成重复业务结果。
- 订单取消、退款、关闭或凭据停用后，原 API Key 不能继续调用。

## 10. 上架验收清单

- 产品元数据完整并通过审核。
- 测试或生产后端地址可从统一 API 网关网络访问并通过健康检查。
- 网关配置保存成功，路由标识唯一。
- 未携带 API Key 的请求返回 `401`。
- 无效或过期 API Key 返回 `401`。
- 有效 API Key 可以正确转发并返回后端响应。
- 超过限流返回 `429`，后端服务未收到被拦截请求。
- 后端不可用时返回 `502`，超时时返回 `504`。
- 平台可以查询调用量、成功率、错误数和平均耗时。
- API Key、企业标识和敏感参数不出现在日志明文中。
