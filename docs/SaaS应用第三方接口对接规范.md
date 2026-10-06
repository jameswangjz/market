# SaaS 应用第三方接口对接规范

版本：v1.1

## 1. 参与方与凭据获取

平台采用“运营平台主动调用第三方 SaaS”的方式。数据集运营服务管理平台为每个审核通过的 SaaS 产品生成 OAuth2 `client_id/client_secret`、Token 地址等接入信息，第三方应用所有者下载凭据文件并在其应用中完成配置；平台使用 OAuth2 `client_credentials` 获取访问令牌。

### 1.1 产品登记时生成什么信息

平台登记产品时会生成平台侧的 `product_id`，用于识别产品；创建订阅时会生成平台侧的 `subscription_id`，用于识别一次独立购买的租户。二者不是 OAuth2 的 `client_id`。

当前实现方式是：SaaS 产品审核通过时，由数据集运营服务管理平台生成该产品的 `client_id`、`client_secret`、Token 地址、Scope 和业务 API 地址配置。产品所有者可以登录平台，在产品列表中重复下载 OAuth 凭据文件，并将其中的信息配置到第三方 SaaS 应用中。文件下载受企业管理员权限保护；测试和生产环境应使用不同客户端。

因此，产品登记时生成产品基础 ID 和接口配置记录；产品审核通过时生成并持久化 OAuth `client_id/client_secret`。第三方 SaaS 需要使用下载的凭据配置其 OAuth2 认证和后续业务接口认证，并按照本文档约定提供 Token 和业务接口。

### 1.2 获取 Token

```http
POST {token_url}
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}&scope={scope}
```

成功响应：

```json
{"access_token":"eyJ...","token_type":"Bearer","expires_in":3600,"scope":"saas.tenant saas.user saas.department"}
```

平台在服务端缓存 Token，并在过期前刷新。第三方不得要求浏览器直接获取 Token，也不得把 `client_secret` 放在前端代码中。

## 2. 业务接口公共约定

```http
POST {base_url}/isv.php
Authorization: Bearer {access_token}
Content-Type: application/json
Accept: application/json
X-Request-Id: {platform_operation_id}
```

平台发送 JSON 请求，所有请求带 `type` 字段，第三方据此分派操作。`X-Request-Id` 是平台操作流水标识，第三方应记录并支持幂等。

成功响应：

```json
{"ret":0,"msg":"ok","request_id":"provider-request-id","data":{}}
```

为兼容参考示例，业务数据也允许直接放在根节点，例如 `tenant_id`、`app_id`、`status`。失败响应：

```json
{"ret":1,"msg":"tenant already exists","code":"TENANT_EXISTS","request_id":"provider-request-id","retryable":false}
```

HTTP 401 表示 Token 无效；HTTP 408、429、5xx 或 `retryable=true` 表示可重试；参数错误、资源不存在、重复业务等错误不得通过重试解决。建议 30 秒内返回。

## 3. 租户接口

### 3.1 开通租户 `OPEN`

请求：

```json
{"type":"OPEN","enterprise_id":"platform-enterprise-id","enterprise_name":"天地奔牛示范企业","product_id":"platform-product-id","subscription_id":"platform-subscription-id","version":"professional","billing_cycle":"annual","contact":{"name":"管理员","email":"admin@example.com","phone":"13800000000"}}
```

成功至少返回：

```json
{"ret":0,"tenant_id":"tenant-10001","app_id":"app-10001","status":"opened","url":"https://saas.example.com/t/tenant-10001"}
```

平台保存 `tenant_id` 和 `app_id`，后续租户操作使用 `tenant_id`。一个企业可以多次开通形成多个独立租户。

### 3.2 关闭租户 `CLOSE`

```json
{"type":"CLOSE","tenant_id":"tenant-10001","subscription_id":"platform-subscription-id"}
```

第三方关闭后至少保留租户和业务数据 1 个月，并返回：

```json
{"ret":0,"tenant_id":"tenant-10001","status":"closed","recover_until":"2026-11-05T00:00:00Z"}
```

### 3.3 续费 `RENEW`

```json
{"type":"RENEW","tenant_id":"tenant-10001","subscription_id":"platform-subscription-id","billing_cycle":"annual","version":"professional"}
```

成功返回新的到期时间。平台规则是续费正常支付，新周期顺延到当前到期时间之后。

### 3.4 版本变更 `CHANGE`

升级或降级请求：

```json
{"type":"CHANGE","tenant_id":"tenant-10001","subscription_id":"platform-subscription-id","from_version":"basic","to_version":"professional","change_type":"upgrade","billing_cycle":"annual"}
```

升级在平台差价订单支付确认后调用；降级按剩余周期比例退款并调用。成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","version":"professional","status":"active"}
```

## 4. 用户和部门接口

### 4.1 增加或恢复用户 `USER_ASSIGN`

```json
{"type":"USER_ASSIGN","tenant_id":"tenant-10001","user_id":"platform-user-id","username":"user@example.com","name":"张三","email":"user@example.com","phone":"13800000000","department_id":"platform-department-id"}
```

删除后的用户再次调用该接口即可恢复。成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","user_id":"external-user-1","status":"active"}
```

### 4.2 删除用户 `USER_UNASSIGN`

```json
{"type":"USER_UNASSIGN","tenant_id":"tenant-10001","user_id":"external-user-1"}
```

成功返回 `status=deleted`。删除不得立即物理清除用户关联数据，以支持恢复。

### 4.3 创建部门 `DEPT_CREATE`

```json
{"type":"DEPT_CREATE","tenant_id":"tenant-10001","department_id":"platform-department-id","parent_id":"","name":"生产部"}
```

成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","department_id":"external-department-1","status":"active"}
```

### 4.4 删除部门 `DEPT_REMOVE`

```json
{"type":"DEPT_REMOVE","tenant_id":"tenant-10001","department_id":"external-department-1"}
```

平台会先处理部门下人员映射，再删除或停用部门。第三方应返回 `status=deleted` 或 `status=disabled`，不得因重复删除返回不可解析错误。

## 5. 生命周期闭环

1. 产品登记生成 `product_id`，配置第三方地址和 OAuth2 凭据。
2. 企业购买某个版本，平台生成 `subscription_id`，调用 `OPEN`。
3. 平台保存第三方 `tenant_id/app_id`，并按需调用部门和用户同步接口。
4. 续费调用 `RENEW`，到期时间顺延。
5. 升级先生成差价订单，支付确认后调用 `CHANGE`；降级调用 `CHANGE` 并生成比例退款记录。
6. 关闭调用 `CLOSE`，30 天内恢复调用 `OPEN` 并携带原租户标识。
7. 所有调用在平台操作台账中可查询，失败可以重试且不会重复产生业务结果。

## 6. 平台接口清单

- `GET/PUT /api/products/{product_id}/saas-integration`
- `GET/POST /api/products/{product_id}/saas-versions`
- `POST /api/products/{product_id}/saas-subscriptions`
- `POST /api/saas-subscriptions/{id}/renew`
- `POST /api/saas-subscriptions/{id}/change-version`
- `POST /api/saas-subscriptions/{id}/close`、`/restore`
- `GET/POST/DELETE /api/saas-subscriptions/{id}/users`
- `GET/POST/DELETE /api/saas-subscriptions/{id}/departments`
- `GET /api/saas-subscriptions/{id}/operations`

## 7. 联调验收清单

- 测试和生产 OAuth 客户端隔离，Token 能获取、过期刷新和拒绝无效密钥。
- `OPEN`、`RENEW`、`CHANGE`、`CLOSE`、恢复接口可以按请求示例闭环。
- 用户删除后再次 `USER_ASSIGN` 可以恢复，部门和部门人员映射可追踪。
- 相同 `X-Request-Id` 重复请求不会产生重复租户、用户或扣费。
- 验证 401、429、超时、5xx、业务失败和重复请求的响应格式。
