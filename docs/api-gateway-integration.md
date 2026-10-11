# API 应用接入统一网关接口与规范

版本：v1.2（2026-10-10 每版本接入门禁）
适用范围：数据集运营服务管理平台中的“API 服务”和“模型 API”产品

## 1. 总体架构

平台采用“统一 API 网关集群 + API 服务独立部署”架构。API 服务提供方独立部署业务 API 服务，平台统一 API 网关负责入口路由、身份认证、企业授权、限流、配额、超时控制和调用统计。

```text
API 提供方业务服务
    ^
    | HTTP/HTTPS 转发
    统一 API 网关集群（Apache APISIX）
    |
    | 按 route_key 路由
    +--> API 服务 A 独立 Service/Deployment
    +--> API 服务 B 独立 Service/Deployment
    +--> API 服务 C 独立 Service/Deployment
```

网关地址：开发联调使用 `http://<平台地址>:30082`，对应 Apache APISIX 数据面；生产环境应通过 HTTPS 域名或 Ingress 暴露，不应直接使用 NodePort。`30081` 为迁移期间的旧 FastAPI 兼容入口，不作为新接入地址。

API 提供方的业务 API 不要求部署在数据集运营服务管理平台所在的 Kubernetes 集群中。`upstream_url` 可以是第三方公网 HTTPS 地址、专用网络地址，或平台 Kubernetes 集群内的 Service 地址；前提是统一 API 网关所在网络能够访问该地址，并完成 TLS、访问控制和健康检查配置。

## 2. API 产品上架流程

1. 在“登记数据或服务”中选择产品类型“API 服务”。
2. 完成产品名称、所属目录、提供方、描述、适用场景、版本、价格策略、安全等级和授权条件等元数据登记。
3. 提供方准备独立部署的 API 服务，并提供网关可访问的后端地址。
4. 平台管理员或产品提供企业管理员配置网关路由。
5. 产品依次通过业务、质量、安全和运营审核，进入“待接入验证”。
6. 平台自动执行各版本健康检查和 APISIX 发布；至少一个版本验证成功后商品发布，只有验证成功的版本可以购买。失败版本修复后重新验证。
7. 提供方根据本规范完成 API 服务端适配并配合验收。

## 3. 网关接入配置接口

本章描述的是产品完成登记、审核和发布后，将 API 服务接入统一网关的控制面接口，不是第三方 API 的业务接口，也不是完整的产品登记接口。

产品登记通过平台产品登记页面完成，登记内容包括产品名称、目录、提供方、描述、版本、适用场景、价格策略、安全等级、授权条件等元数据。API 提供方需要先向平台提供以下接入信息：

- 可从统一网关访问的 `upstream_url`；
- API 路由标识和业务版本；
- 支持的 HTTP 方法、路径、请求参数和响应格式；
- 健康检查地址、超时要求和访问控制要求；
- 文件或流式响应约定；
- 限流、每日配额和月度配额建议值。

产品完成审核发布后，平台会对已保存的网关后端地址执行健康检查；检查通过后自动发布网关路由，状态为 `active`。检查失败时状态为 `publish_failed`，产品仍可审核通过，但不会对外转发；修正配置后可重新执行检查。具备平台授权的产品提供企业管理员可以调用以下控制面接口完成配置，但第三方 API 应用本身不需要实现这些接口。

所有平台控制面接口基础路径为 `/api`，需要平台登录令牌。

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
  "strip_prefix": true,
  "health_path": "/health",
  "health_method": "GET",
  "upstream_auth_mode": "oauth2",
  "upstream_scope": "resource.invoke"
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
| `monthly_quota` | 否 | 版本每订阅月调用次数；`0` 表示月度不限额，每日限额独立生效 |
| `timeout_ms` | 否 | 网关等待后端响应的最大时间，范围 100 至 120000 毫秒 |
| `strip_prefix` | 否 | 是否只将路由后的路径转发给后端服务 |
| `health_path` | 否 | 审核后健康检查路径，默认 `/health` |
| `health_method` | 否 | 健康检查方法，仅支持 `GET` 或 `HEAD` |
| `upstream_auth_mode` | 否 | 网关访问提供方服务的认证方式，默认 `oauth2`，也可为 `none` |
| `upstream_scope` | 否 | 平台 Token 请求的作用域 |

### 3.2 发布网关路由

```http
POST /api/products/{product_id}/gateway-config/publish
Authorization: Bearer <platform-token>
```

产品必须已经完成产品审核并处于“已发布”状态，网关路由才可以启用。

### 3.3 审核后自动健康检查

```http
POST /api/products/{product_id}/gateway-config/health-check
Authorization: Bearer <platform-token>
```

平台审核接口在审核结论为通过时会自动执行同样的健康检查。健康检查返回 2xx 后，路由自动变为 `active`；检查中为 `pending_health`，失败为 `publish_failed`。产品版本可以分别定义 `rate_limit_per_minute`、`daily_quota`、`monthly_quota`，订单凭据按购买版本继承对应策略，凭据自身配置不为空时优先使用凭据策略。

### 3.4 平台统一 OAuth2

API 提供方和 SaaS 应用统一作为平台 OAuth2 资源服务。产品审核通过后，平台为产品生成 OAuth 客户端，并由统一网关或 SaaS 调用模块使用 `client_credentials` 获取平台访问令牌。提供方服务不再自行提供 Token 地址，也不使用调用方的 API Key 作为后端认证。

平台 Token 接口：

```http
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<platform-client-id>&client_secret=<platform-client-secret>&scope=resource.invoke
```

提供方服务应从 `Authorization: Bearer <platform-access-token>` 读取令牌，并通过平台 `POST /oauth/introspect` 校验。当前版本以 introspection 为标准校验方式；平台后续可增加 JWKS 公钥校验方式。平台 Token 的 `aud` 为 `market-resource`，并包含产品标识、客户端标识和作用域。

产品审核通过后，产品所有者可在产品页面下载统一 OAuth2 接入配置文件：

```http
GET /api/products/{product_id}/oauth-credentials-download
Authorization: Bearer <platform-login-token>
```

下载文件包含 `client_id`、`client_secret`、Token 地址、校验地址、Issuer、Audience 和 Scope，产品提供方应仅在服务端安全保存敏感字段。

网关转发到提供方时会自动获取、缓存和刷新 Token，并将 Token 放入上游请求的 `Authorization` 请求头；调用方传入的认证头不会直接转发。提供方服务应至少校验 Token 有效期、签发方、受众、产品标识和 `resource.invoke` 作用域。

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

## 5. 网关转发与服务端行为

### 5.1 限流与配额行为

- Redis 保存分布式分钟、日和订阅月计数。
- 新订阅以订单冻结的版本策略为准，不能由买方任意覆盖审核后的限额。
- 超过分钟限流或每日配额时，网关返回 `429`，不会转发到后端服务。
- 新订阅的 `monthly_quota` 按原始服务生效日划分订阅月；目标月无对应日期时取月末，后续仍按原日期计算。日限额按北京时间自然日重置。企业与产品共享计数，同版本续费只延长期限，不叠加或重置当前配额；轮换凭据不清零用量。超过配额返回 `429`。
- 历史订单暂保留旧计数模式，有历史用量的凭据迁移新模式须经过单独校验，不允许直接清零切换。本阶段的正常购买仅支持同版本续费，跨版本升降级仍待后续联调。
- 平台调用策略由平台运营人员配置，提供方应能够正确处理平台返回的 429。

### 5.3 参数、幂等和内容类型

- 路径参数和查询参数按产品 API 定义传递，网关不会修改业务参数。
- `Content-Type`、`Accept`、`Content-Encoding` 等非认证请求头会按原值转发；认证头不会转发给后端。
- `X-Request-Id` 会由网关透传或生成，提供方应在日志和业务响应中保留该标识；写操作应支持 `Idempotency-Key`。
- 第三方服务应保证相同幂等号的重复写请求不会重复扣费、重复创建或重复变更。

### 5.4 文件、大响应和流式响应

- 文件下载接口应返回正确的 `Content-Type`、`Content-Disposition` 和文件名，网关透传响应头和二进制内容，不应将文件内容转换为 JSON。
- 大响应建议使用分页、压缩或异步任务；第三方服务应在登记资料中说明最大响应大小和超时要求。
- 当请求带有 `X-Market-Stream: true` 或 `Accept: text/event-stream` 时，网关以流式方式转发数据，不等待完整响应；提供方应定期发送心跳并处理客户端断开。
- 流式响应仍受网关超时和配额策略限制，提供方应在接口文档中说明流式事件格式和最大持续时间。

### 5.5 错误和重试约定

网关和第三方服务建议统一返回：

```json
{"code":"QUOTA_EXCEEDED","message":"超过 API 每日调用配额","request_id":"req-001","retryable":false}
```

提供方应按 HTTP 状态码和统一错误 JSON 返回可解析的业务错误，并明确 `retryable`。统一 API 网关当前不自动重试业务请求，提供方不得依赖网关重试来完成业务操作；幂等写请求必须支持使用相同幂等号查询或复用最终结果。

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
- 提供方不得要求调用方把 API Key 写入业务请求体；网关认证信息不得写入服务日志。
- 提供方不得记录或保存调用方的完整认证信息。
- API 服务只允许网关网络访问，避免绕过网关直接暴露。
- 敏感参数不得出现在普通日志中。
- API 服务应自行完成业务级数据权限、参数校验和敏感数据脱敏。
- 涉及国密要求的 API，应在产品登记和安全合规审核中明确 SM2/SM3/SM4 使用边界。

## 8. 版本、变更和下线

- 每个 API 产品应使用稳定的 `route_key`，业务版本通过 `/v1`、`/v2` 等路径区分。
- 新版本上线时应先创建新版本路由并完成联调，再通知调用方切换；旧版本至少保留一个双方约定的兼容期。
- 路由地址、认证方式、响应结构等不兼容变更必须提前通知，并提供迁移说明。
- API 下线前应配合平台完成下线通知和迁移；紧急安全下线时应提供影响范围、替代地址和恢复方案。

## 9. 测试环境与联调验收

开发环境网关地址：`http://192.168.10.10:30082`。生产环境必须使用 HTTPS 域名或 Ingress；`30081` 仅用于旧网关回退验证。

联调步骤：

1. API 提供方在平台登记 API 产品，提供可访问的测试后端地址，配置路由和限流策略。
2. 产品审核发布并启用路由，由平台集成管理员提供临时测试凭据。
3. 提供方使用网关健康检查 `GET /health` 和测试路由完成正常请求、文件、流式、超额、后端 502 和超时 504 联调。
4. 在平台调用统计中核对调用次数、成功率、HTTP 状态码、平均耗时和请求/响应字节数。

最低验收标准：

- 有效网关请求可以访问正确路由，企业身份请求头能够传递到后端。
- 分钟、每日和月度配额分别生效，超额请求返回 429 且不转发到后端。
- 文件内容、响应头和流式数据能够正常透传。
- 后端不可用返回 502，后端超时返回 504，调用统计仍保留失败记录。
- 相同 `X-Request-Id` 或 `Idempotency-Key` 的写请求不会造成重复业务结果。

## 10. 上架验收清单

- 产品元数据完整并通过审核。
- 测试或生产后端地址可从统一 API 网关网络访问并通过健康检查。
- 网关配置保存成功，路由标识唯一。
- 平台认证失败请求不会转发到后端。
- 有效网关请求可以正确转发并返回后端响应。
- 超过限流返回 `429`，后端服务未收到被拦截请求。
- 后端不可用时返回 `502`，超时时返回 `504`。
- 平台可以查询调用量、成功率、错误数和平均耗时。
- 认证信息、企业标识和敏感参数不出现在日志明文中。

## 11. 每版本接入与重试接口

本节仅面向 API/模型 API 提供方及平台集成管理员，不涉及购买方凭据。平台沿用统一 OAuth2 授权服务器；提供方接入凭据与购买方调用网关凭据完全分开。资源服务继续验证平台签发的访问令牌，不能把接入 client_secret 放入公开产品详情。

接口需携带平台登录 Bearer Token，操作人员为产品提供企业的活跃超级管理员、企业管理员，或平台超级管理员、平台运营人员：

| 方法 | 地址 | 用途 |
| --- | --- | --- |
| PUT | `/api/products/{product_id}/versions/{version_id}/integration` | 保存指定版本接入配置 |
| GET | 同上 | 查看配置状态、健康结果、路由信息；不返回 client_secret |
| POST | 上述地址加 `/verify` | 完成四审后验证和发布；失败返回状态 `failed`，不能购买该版本 |

PUT 请求例：

```json
{"upstream_url":"https://api.example.com","route_key":"example-v1","version":"v1","auth_mode":"api_key","upstream_auth_mode":"oauth2","upstream_scope":"resource.invoke","rate_limit_per_minute":60,"daily_quota":10000,"monthly_quota":100000,"timeout_ms":30000,"strip_prefix":true,"health_path":"/health","health_method":"GET"}
```

`version` 必须与登记的版本号一致，`route_key` 在所有版本间唯一。每天、每月配额为 0 表示不限制；每分钟限流仍须正整数。网关代理使用配置的超时时长，接入健康探测单次最长 10 秒，不跟随重定向；健康检查应返回 2xx。供应方应提供无需个人登录的只读健康检查端点。

成功验证结果包含 `version_id`、`route_id`、`status=verified`、`healthy=true`、`health_status_code`、`verified_at`；POST 另外返回 `product_status`。失败必须查看上述状态，HTTP 200 本身不代表验证通过。运营审核时自动尝试已有配置；无专属配置时，以登记上游地址和该版本策略生成配置。需要不同上游、Scope、健康路径的版本应提前保存专属配置。

已售版本禁止覆盖接入和策略配置，应登记新版本。旧产品单路由及旧订单 ID 保留兼容；对多版本产品调用旧 `gateway-config` 读取、验证、发布、回滚或调用统计接口时，必须增加 `?version=v1`，否则返回 409，避免误操作另一版本。

私网后端需由平台运维显式配置 `UPSTREAM_ALLOWED_HOSTS`（精确主机名）或 `UPSTREAM_ALLOWED_CIDRS`（网段）；不能授权回环、链路本地及云元数据地址。探测验证全部 DNS 地址，并固定 IP、保留 Host 和 TLS SNI，APISIX 新接入路由也使用验证后的 IP。地址变更需重新验证；访问控制配置只应由运维调整，不由产品提交者放宽。
