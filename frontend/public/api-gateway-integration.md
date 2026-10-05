# API 应用接入统一网关接口与规范

版本：v1.0  
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

## 2. API 产品上架流程

1. 在“登记数据或服务”中选择产品类型“API 服务”。
2. 完成产品名称、所属目录、提供方、描述、适用场景、版本、价格策略、安全等级和授权条件等元数据登记。
3. 提供方准备独立部署的 API 服务，并提供网关可访问的后端地址。
4. 平台管理员或产品提供企业管理员配置网关路由。
5. 产品通过平台审核并发布。
6. 发布网关路由，生成企业专属 API Key。
7. 调用方按照本规范访问统一 API 网关。

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
  "expires_at": "2027-12-31T23:59:59Z"
}
```

API Key 只在生成接口响应中返回一次。平台数据库只保存不可逆哈希，不保存密钥原文。

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
- 调用方应采用指数退避，不应持续重试造成流量放大。
- 生产调用应设置业务幂等号，例如 `X-Request-Id` 或 `Idempotency-Key`。

## 6. Kubernetes 独立部署要求

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

## 8. 上架验收清单

- 产品元数据完整并通过审核。
- 后端 Service 在 Kubernetes 中 Ready。
- 网关配置保存成功，路由标识唯一。
- 未携带 API Key 的请求返回 `401`。
- 无效或过期 API Key 返回 `401`。
- 有效 API Key 可以正确转发并返回后端响应。
- 超过限流返回 `429`，后端服务未收到被拦截请求。
- 后端不可用时返回 `502`，超时时返回 `504`。
- 平台可以查询调用量、成功率、错误数和平均耗时。
- API Key、企业标识和敏感参数不出现在日志明文中。
