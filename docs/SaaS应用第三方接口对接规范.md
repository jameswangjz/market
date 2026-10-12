# SaaS 应用第三方接口对接规范

版本：v1.2

## 1. 参与方与凭据获取

平台采用“运营平台主动调用第三方 SaaS”的方式。数据集运营服务管理平台为每个审核通过的 SaaS 产品生成 OAuth2 `client_id/client_secret`、Token 地址等接入信息，第三方应用所有者下载凭据文件并在其应用中完成配置；平台使用 OAuth2 `client_credentials` 获取访问令牌。

### 1.1 产品登记时生成什么信息

平台登记产品时会生成平台侧的 `product_id`，用于识别产品；创建订阅时会生成平台侧的 `subscription_id`，用于识别一次独立购买的租户。二者不是 OAuth2 的 `client_id`。

当前实现方式是：SaaS 产品审核通过时，由数据集运营服务管理平台生成该产品的 OAuth2 接口客户端和 OIDC 登录客户端。提供方企业超级管理员、企业管理员可登录平台，在产品列表中重复下载一份 SaaS 应用接入配置 JSON 文件，按文件内的两个分区配置服务端。测试和生产环境应使用不同客户端。

因此，产品登记时生成产品基础 ID 和接口配置记录；产品审核通过时生成并持久化 OAuth `client_id/client_secret`。第三方 SaaS 是资源服务，须校验平台签发的 Token 并提供业务接口，不需要为平台提供独立的 Token 授权服务器。

### 1.3 发布时自动生成两套凭据

四阶段审核通过后进入“待接入验证”，平台在审核事务内自动创建互相独立的 OAuth2 接口客户端和 OIDC 登录客户端。提供方企业的已实名、已激活超级管理员和管理员，可在产品页面点击“下载接入配置”，一次下载完整配置：

- `GET /api/products/{product_id}/saas-integration-credentials-download`：需携带平台登录 Bearer Token；返回禁止缓存的 JSON 附件。购买方、企业普通成员、平台内部角色均无权下载。历史两条单独下载接口仅保留兼容，不再作为 SaaS 前端入口。
- `oauth2_api`：平台调用管理接口的机器客户端，Scope 为 `saas.tenant saas.user saas.department`；包含客户端、Token 地址、校验地址、Issuer、Audience。平台使用此配置获取机器 Token，SaaS 资源服务负责验证该 Token，不用于用户登录。
- `oidc_sso`：SaaS 服务端登录客户端；包含客户端、Issuer、发现地址、授权地址、Token 地址、UserInfo 地址、JWKS 地址、已登记回调地址和 PKCE 参数。
- `instructions`：用途区分与安全配置说明。文件不包含买方 API 网关凭据，不是购买方授权文件。

文件结构示意（占位符不是实际凭据，接入时使用平台下载的完整值）：

```json
{
  "schema_version": "1.0",
  "product": {"id": "product-id", "name": "SaaS产品名称"},
  "oauth2_api": {
    "client_id": "machine-client-id", "client_secret": "machine-secret",
    "grant_type": "client_credentials", "token_url": "https://platform.example/oauth/token",
    "introspection_url": "https://platform.example/oauth/introspect",
    "issuer": "https://platform.example", "audience": "market-resource",
    "scope": "saas.tenant saas.user saas.department"
  },
  "oidc_sso": {
    "client_id": "login-client-id", "client_secret": "login-secret",
    "grant_type": "authorization_code", "code_challenge_method": "S256",
    "issuer": "https://platform.example",
    "discovery_url": "https://platform.example/.well-known/openid-configuration",
    "authorization_url": "https://platform.example/oidc/authorize",
    "token_url": "https://platform.example/oidc/token",
    "userinfo_url": "https://platform.example/oidc/userinfo",
    "jwks_url": "https://platform.example/oidc/jwks",
    "redirect_uris": ["https://saas.example/sso/callback"],
    "response_type": "code", "scopes": ["openid", "profile", "email"]
  },
  "instructions": ["仅服务端保存两套独立凭据，不向购买方提供密钥"]
}
```

两个客户端均绑定 SaaS 产品，同一产品的买方共享登录客户端配置，但授权码、令牌、用户身份及租户权限独立。买方无需配置提供方的 Client Secret。任何一套客户端停用、缺失或密钥不可恢复时，统一下载返回 409，不提供不完整文件，也不自动生成新密钥。

OIDC 使用授权码流程和 PKCE S256，Scope 为 `openid profile email`；SaaS 必须核验 state、nonce、ID Token 签名及 issuer/audience，并按企业和租户隔离用户会话。不能把 client_credentials 令牌当作用户登录令牌。

两套密钥不得混用，不得发送给购买方或写入浏览器代码。下载可重复进行，响应禁止缓存并记录审计。重复发布不自动轮换凭据；历史仅保存摘要的 OIDC 密钥不可恢复，须经过受控轮换。

提供方完成服务器配置后，调用 `POST /api/products/{product_id}/saas-integration/verify` 验证接入。该检查验证平台取 Token、管理接口健康及 OIDC 登记配置，不替代最终用户浏览器 SSO 联调。产品通过接入检查后正式可购买。

系统配置的“应用接入地址允许HTTP”默认关闭。明确开启后，允许登记 HTTP 应用地址、管理接口地址及回调地址，但仍执行 URL 安全校验、回调精确匹配及上游 SSRF 防护；正式生产环境推荐 HTTPS。

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


## 8. 用户单点登录（OIDC）

本节用于用户浏览器登录，不能替代前文机器接口的 `client_credentials`。SaaS 作为 OIDC 客户端和资源服务，平台作为 OP，采用授权码 + PKCE S256。用户 SSO 客户端与产品管理接口客户端必须分开配置，禁止把机器 Token 当作用户身份。

### 8.1 登记与客户端配置

产品登记时填写 SaaS 应用访问地址和精确 OIDC 回调地址。四阶段审核通过后平台自动创建独立用户登录客户端，无需提供方手工调用创建接口。提供方企业超级管理员、管理员通过第 1.3 节统一下载文件的 `oidc_sso` 分区获得配置，可重复下载，不轮换密钥。`POST /api/products/{product_id}/oidc-client` 仅作为受控兼容配置接口；其创建时的临时返回不替代统一下载功能。客户端密钥必须由服务端安全保存，浏览器不得保存 Secret 或 OIDC Token。

发现文档：`GET {issuer}/.well-known/openid-configuration`；公钥：`GET {issuer}/oidc/jwks`。生产回调使用 HTTPS，精确匹配白名单，不允许通配符；localhost HTTP 仅限显式开启的开发测试。

### 8.2 登录与回调

用户从平台“我的应用”进入登记的应用访问地址，附带 `enterprise_id` 企业上下文；RP 不得仅凭此参数认定用户身份或授予租户权限。

RP 服务端生成随机 state、nonce、PKCE verifier，保存短期一次性事务并绑定 HttpOnly Cookie。浏览器访问：

```http
GET {issuer}/oidc/authorize?response_type=code&client_id={oidc_client_id}&redirect_uri={registered_callback}&scope=openid%20profile%20email&state={random_state}&nonce={random_nonce}&code_challenge={S256_challenge}&code_challenge_method=S256&enterprise_id={buyer_enterprise_id}
```

平台引导登录并由用户明确同意或拒绝。授权回调成功返回 `code`、原 state；拒绝返回 `error=access_denied`、原 state。RP 校验 Cookie、state、事务期限和一次性状态后，由服务端换码：

```http
POST {issuer}/oidc/token
Authorization: Basic {base64(oidc_client_id:oidc_client_secret)}
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&code={code}&redirect_uri={registered_callback}&code_verifier={original_verifier}
```

成功返回 Bearer `access_token`、`id_token` 和 `expires_in`，无 refresh_token。授权码有效期 120 秒，访问令牌及 ID Token 约 300 秒；错误或过期后应重新发起登录，不能重复使用授权码。

### 8.3 验签与租户会话

RP 使用成熟 OIDC 库，以受信任发现文档及 JWKS 验证 RS256 签名、issuer、audience、exp、nonce、at_hash；不得接受其他签名算法或由 Token 自行指定的外部密钥地址。使用 `GET {issuer}/oidc/userinfo` 和 Bearer 用户令牌再次获取身份。

ID Token 与 userinfo 的 `sub`、`enterprise_id`、`subscription_id`、`tenant_id` 必须一致，并匹配本次企业上下文和已通过 OPEN 开通的租户。映射使用 issuer + sub + 企业 + 租户，不得仅按邮箱匹配已有账户。

验证通过后 RP 创建独立 HttpOnly 会话；会话不得超过用户令牌期限。受保护请求需重新校验平台 userinfo 和本地租户状态；用户禁用、授权撤回、订阅到期、安全下架及租户关闭均拒绝访问。用户退出只清除 RP 会话，不意味着平台账户全局退出。平台、RP 审计和访问日志不得记录 Secret、Token 或授权码。

### 8.4 验收边界

必须验证真实浏览器登录、同意/拒绝、错误 state/nonce/PKCE、错误 issuer/audience/签名、授权码重放、跨企业拒绝、普通成员授权撤回和到期/禁用拒绝。开发模拟 RP 使用本地持久化会话及实际平台协议；这不代表第三方生产系统已经完成联调。多副本 RP 应使用共享会话存储，不能直接复制本开发用单副本 SQLite 部署。
