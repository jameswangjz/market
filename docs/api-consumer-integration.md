# API 应用调用方接入与调用规范

版本：v1.0

本文件面向购买或准备购买 API 服务、模型 API 的用户和企业应用开发人员。用户可以在购买前查阅本文件，了解接入方式、凭据管理、调用地址、配额和错误处理规则。

## 1. 接入方式

平台采用“统一 API 网关集群 + API 服务独立部署”架构。调用方不直接访问 API 提供方，而是访问平台统一网关：

```text
调用方应用 -> 统一 API 网关 -> API 提供方业务服务
```

开发环境网关地址：`http://192.168.10.10:30081`。生产环境使用平台提供的 HTTPS 域名。

产品详情或订单中会提供以下信息：

- 网关地址；
- `route_key`；
- API 版本和路径；
- 调用方式和请求参数；
- 当前产品的配额和超时时间。

## 2. 购买前确认

购买前应确认：

1. API 产品的版本、价格和授权范围；
2. 是否支持文件、JSON 或流式响应；
3. 每分钟、每日和每月调用配额；
4. API 版本和兼容期；
5. 是否需要企业身份、数据权限或国密算法支持；
6. 业务接口的请求参数、响应字段和错误码。

API 提供方的服务可以部署在独立 Kubernetes 集群、虚拟机、物理服务器或云主机中，调用方不需要关心其部署位置。

## 3. 凭据获取和生命周期

订单完成支付后，订单所有者可以进入订单详情页的“API 调用凭据”区域：

- 已实名个人用户可以管理本人购买订单的凭据；
- 已实名企业的企业超级管理员可以管理企业购买订单的凭据；
- 企业管理员可以执行企业内部凭据运维操作。

首次获取凭据时，平台生成 API Key 并只在本次操作结果中显示原文。调用方必须立即保存，不要把 API Key 写入前端代码、镜像或公开代码仓库。

订单页面也支持停用和重新生成凭据。重新生成会立即停用旧凭据；订单取消、退款、关闭、凭据过期或凭据停用后，网关将拒绝调用。

订单页面接口如下：

```http
GET  /api/orders/{order_id}/api-credentials
POST /api/orders/{order_id}/api-credentials
POST /api/orders/{order_id}/api-credentials/{credential_id}/revoke
POST /api/orders/{order_id}/api-credentials/{credential_id}/regenerate
```

以上为平台登录后的管理接口，实际使用时优先通过订单详情页面操作。

## 4. 网关调用

调用地址格式：

```text
{gateway_base_url}/gateway/{route_key}/{api_path}
```

使用 `X-API-Key`：

```http
GET /gateway/quality-api/v1/quality-score?equipment_id=EQ-001 HTTP/1.1
Host: gateway.example.com
X-API-Key: mk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
X-Request-Id: req-20261006-0001
Accept: application/json
```

也可以使用 Bearer：

```http
Authorization: Bearer mk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

平台网关不会把认证头转发给第三方 API。认证成功后，网关会向 API 提供方注入：

```http
X-Market-Enterprise-Id: <调用企业ID>
X-Market-Route-Key: <网关路由标识>
```

调用方不得伪造或依赖直接传递企业身份，企业身份以平台网关鉴权结果为准。

## 5. 请求和响应

支持 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`HEAD` 和 `OPTIONS`。业务参数按照 API 提供方的接口说明放在路径、查询参数或 JSON 请求体中。

推荐响应格式：

```json
{
  "code": 0,
  "message": "success",
  "data": {},
  "request_id": "req-20261006-0001"
}
```

写操作必须携带 `X-Request-Id`，建议同时携带 `Idempotency-Key`。调用方重试时应使用相同幂等号，避免重复创建、扣费或变更。

文件接口应保留响应中的 `Content-Type`、`Content-Disposition` 和文件名。大响应建议使用分页、压缩或异步任务。

SSE 或其他流式接口调用时增加：

```http
X-Market-Stream: true
```

或设置：

```http
Accept: text/event-stream
```

## 6. 限流和配额

平台支持：

- 每分钟调用频率限制；
- 每日调用配额；
- 每月调用配额；
- 产品和 API Key 独立策略；
- 配额超限返回 HTTP 429。

调用方不应通过并发放大绕过配额。收到 429 时应读取 `Retry-After`（如有），或采用指数退避。

## 7. 错误和重试

```json
{
  "code": "QUOTA_EXCEEDED",
  "message": "超过 API 每日调用配额",
  "request_id": "req-001",
  "retryable": false
}
```

| 状态码 | 说明 | 建议 |
|---|---|---|
| 401 | 凭据缺失、错误、过期或停用 | 不重试，检查凭据 |
| 403 | 订单授权或业务权限不足 | 不重试，检查产品授权 |
| 404 | 路由或资源不存在 | 不重试，检查版本和路径 |
| 409 | 幂等或业务状态冲突 | 使用相同幂等号查询结果 |
| 429 | 超过限流或配额 | 退避后重试 |
| 502 | API 提供方不可用 | 幂等请求最多重试 3 次 |
| 504 | API 提供方超时 | 幂等请求最多重试 3 次 |
| 5xx | 服务端错误 | 仅对幂等请求重试 |

平台网关当前不自动重试业务请求。调用方应自行控制重试次数和退避时间，非幂等请求必须先查询上一次请求结果。

## 8. 版本和变更

- API 版本通常通过 `/v1`、`/v2` 等路径区分；
- 新版本不代表旧版本订单自动获得访问权限；
- 发生不兼容变更时，API 提供方应发布迁移说明和兼容期；
- 调用方应避免硬编码不可变更的后端地址，只使用平台提供的网关地址和路由；
- API 下线或订单到期后，原凭据不能继续调用。

## 9. 联调示例

```bash
curl -X GET \
  'http://192.168.10.10:30081/gateway/quality-api/v1/quality-score?equipment_id=EQ-001' \
  -H 'X-API-Key: <订单页面生成的 API Key>' \
  -H 'X-Request-Id: test-001' \
  -H 'Accept: application/json'
```

验收时应验证：正常调用、错误凭据、停用凭据、过期凭据、订单授权失效、超过限流、超过每日/月度配额、文件下载、流式响应、502 和 504。

