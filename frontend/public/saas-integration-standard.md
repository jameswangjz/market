# SaaS 应用第三方接口对接规范

版本：v1.3；接入配置 JSON 格式：2.0。

## 1. 参与方与凭据获取

平台采用“运营平台主动调用第三方 SaaS”的方式。平台仍为每个审核通过的 SaaS 产品生成并保存机器 OAuth2 客户端ID和密钥，用 `client_credentials` 向平台自身获取机器 Token，再调用 SaaS 管理接口。这组机器凭据仅供平台内部使用，不再包含在第三方下载文件中。SaaS 方获得 OIDC 用户登录凭据及机器 Token 验证元数据。

### 1.1 产品登记时生成什么信息

平台登记产品时生成 `product_id`；购买后按企业与产品组合绑定订阅及租户，续费复用现有组合。`subscription_id` 识别订阅，不是 OAuth2 或 OIDC 客户端ID，不能假定每次续费新建一个租户。

SaaS 产品完成四阶段审核后，平台自动生成或复用产品级机器 OAuth2 客户端与独立的 OIDC 登录客户端。提供方企业超级管理员、企业管理员在产品列表中下载 SaaS 应用接入配置 JSON，按 `resource_server` 配置机器接口的 Token 校验，按 `oidc_sso` 配置用户登录。测试和生产环境应使用不同客户端。

第三方 SaaS 是机器接口的资源服务和用户登录的 OIDC 客户端，不需要为平台提供另一个机器 Token 授权服务器，也不需要持有平台内部机器客户端密钥。

### 1.2 下载配置与内部凭据的边界

四阶段审核通过后进入“待接入验证”，平台在审核事务内自动创建互相独立的 OAuth2 接口客户端和 OIDC 登录客户端。提供方企业的已实名、已激活超级管理员和管理员，可在产品页面点击“下载接入配置”，一次下载完整配置：

- `GET /api/products/{product_id}/saas-integration-credentials-download`：需携带平台登录 Bearer Token；返回禁止缓存的 JSON 附件。购买方、企业普通成员、平台内部角色均无权下载。
- `resource_server`：SaaS 管理接口需要的 Bearer Token 验证方式、内省地址、Issuer、Audience及允许的Scope；不包含机器客户端ID、密钥或平台内部取Token地址。
- `oidc_sso`：SaaS 服务端登录客户端；包含客户端、Issuer、发现地址、授权地址、Token 地址、UserInfo 地址、JWKS 地址、已登记回调地址和 PKCE 参数。
- `instructions`：用途区分与安全配置说明。文件不包含买方 API 网关凭据，不是购买方授权文件。

文件结构示意（占位符不是实际凭据，接入时使用平台下载的完整值）：

```json
{
  "schema_version": "2.0",
  "product": {"id": "product-id", "name": "SaaS产品名称"},
  "resource_server": {
    "authentication": "Bearer", "validation_method": "introspection",
    "introspection_url": "https://platform.example/oauth/introspect",
    "issuer": "https://platform.example", "audience": "market-resource",
    "scopes": ["saas.department", "saas.tenant", "saas.user"]
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
  "instructions": ["平台内部机器凭据不下发；OIDC密钥仅由SaaS服务端保存，不向购买方提供"]
}
```

两个客户端仍绑定 SaaS 产品，同一产品的买方共享登录客户端配置，但授权码、令牌、用户身份及租户权限独立。买方无需配置提供方的 Client Secret。下载仍检查平台内部机器客户端及 OIDC 客户端有效性，不会因隐藏机器凭据而取消平台认证检查，也不会在下载时自动生成或轮换密钥。

OIDC 使用授权码流程和 PKCE S256，Scope 为 `openid profile email`；SaaS 必须核验 state、nonce、ID Token 签名及 issuer/audience，并按企业和租户隔离用户会话。不能把 client_credentials 令牌当作用户登录令牌。

OIDC 密钥不得发送给购买方或写入浏览器代码。下载可重复进行，响应禁止缓存并记录审计。重复发布不自动轮换凭据；历史仅保存摘要的 OIDC 密钥不可恢复，须经过受控轮换。

格式 2.0 不再返回 `oauth2_api`。已有接入应改读 `resource_server`，不再从文件读取机器密钥。旧文件无法远程收回，本次格式更新不会自动轮换其中的旧机器密钥。SaaS 历史 `/oauth-credentials-download` 入口返回 410，避免绕过统一下载继续取得机器凭据；API 产品的对应下载行为不受此项调整影响。

提供方完成服务器配置后，调用 `POST /api/products/{product_id}/saas-integration/verify` 验证接入。该检查验证平台取 Token、管理接口健康及 OIDC 登记配置，不替代最终用户浏览器 SSO 联调。产品通过接入检查后正式可购买。

系统配置的“应用接入地址允许HTTP”默认关闭。明确开启后，允许登记 HTTP 应用地址、管理接口地址及回调地址，但仍执行 URL 安全校验、回调精确匹配及上游 SSRF 防护；正式生产环境推荐 HTTPS。

### 1.3 SaaS 如何验证平台的机器 Token

以下调用由 **SaaS 服务端** 在收到平台管理请求后执行。SaaS 无需向平台提交机器客户端ID或密钥来获取 Token；平台已经在其后台完成 `client_credentials` 取 Token。

```http
POST {resource_server.introspection_url}
Content-Type: application/x-www-form-urlencoded

token={received_bearer_token}
```

有效响应示意：

```json
{"active":true,"product_id":"product-id","iss":"https://platform.example","aud":"market-resource","scope":"saas.tenant saas.user saas.department"}
```

SaaS 必须拒绝内省失败、无法访问内省服务或 `active` 不为 true 的请求；核对返回的产品等于本地接入产品，issuer、audience符合 `resource_server`，操作所需Scope在返回的空格分隔scope中。之后继续校验业务请求的租户、企业、订阅和幂等条件，不能只检查Token有效就执行任意操作。

当前机器 Token 是 HS256 JWT，平台负责验签及客户端／撤销检查；SaaS 通过内省验证，不使用 OIDC JWKS 验证机器 Token，也不使用 OIDC client_secret 作为机器签名密钥。OIDC JWKS 仅用于相应的 RS256 ID Token 校验。当前内省接口未要求额外的资源服务器客户端认证，生产部署须明确网络访问控制、TLS及调用方认证方案，不能自行假设已启用 mTLS。

平台会缓存机器 Token，收到 SaaS HTTP 401 后清除对应旧缓存，后续业务重试再取新 Token；不会在单次失败请求中立即重放。SaaS 应以401表达Token无效，以403表达权限不足。SaaS 不需处理平台取Token的内部密钥及缓存。

开发环境配置中可能出现 K8S 内部地址 `market-api:8000`；集群外的真实 SaaS 必须获得可达内省地址。issuer是须精确匹配的签发标识，不能擅自用公网地址替换已签发Token中的内部issuer。正式接入前需由平台统一协调可达地址及稳定issuer。

## 2. 业务接口公共约定

```http
POST {base_url}/isv.php
Authorization: Bearer {access_token}
Content-Type: application/json
Accept: application/json
X-Request-Id: {optional_operation_id}
```

平台发送 JSON 请求，所有请求带 `type` 字段，第三方据此分派操作。当前通用调用主要通过 JSON 的 `idempotency_key` 传递幂等键，不能要求所有请求必有 `X-Request-Id`。若收到该可选请求头，应与 JSON 幂等键一致，否则拒绝；第三方须记录产品范围内的幂等键并防止同键不同请求重复执行。

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
{"type":"OPEN","enterprise_id":"platform-enterprise-id","enterprise_name":"天地奔牛示范企业","product_id":"platform-product-id","subscription_id":"platform-subscription-id","version":"professional","billing_cycle":"annual","starts_at":"2026-10-15T08:00:00+00:00","expires_at":"2027-10-15T08:00:00+00:00","admin_user_id":"platform-admin-user-id","oidc_client_id":"product-oidc-client-id","contact":{"name":"管理员","email":"admin@example.com","phone":"13800000000"}}
```

成功至少返回：

```json
{"ret":0,"tenant_id":"tenant-10001","app_id":"app-10001","status":"opened","url":"https://saas.example.com/t/tenant-10001"}
```

平台保存 `tenant_id` 和 `app_id`，后续租户操作使用 `tenant_id`。一个企业可以多次开通形成多个独立租户。

平台自动交付会传入上述产品、企业、订阅、管理员、OIDC 客户端及订阅时间配置。第三方必须保留身份及到期时间；`oidc_client_id` 必须使用统一接入配置中 `oidc_sso.client_id`，不是平台内部机器客户端标识。`admin_user_id` 是平台用户 ID，必须与后续 USER_ASSIGN 的平台用户及活跃管理员映射一致，不得用第三方用户 ID 替换。完成管理员同步和应用启用后，STATUS 必须按下述完整字段证明交付状态。

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
{"type":"CHANGE","tenant_id":"tenant-10001","subscription_id":"platform-subscription-id","from_version":"basic","to_version":"professional","change_type":"upgrade","billing_cycle":"annual","change_id":"change-10001","direction":"target","idempotency_key":"change-10001-target"}
```

升级在平台差价订单支付确认后调用；降级切换及退款核定分别留证，不表示自动执行退款。成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","version":"professional","status":"active","change_id":"change-10001","direction":"target","effective_at":"2026-10-15T08:00:00.123456+00:00"}
```

`change_id` 是平台版本切换标识，响应必须原样返回；`direction` 必须是 `target`（切换目标版本）或 `original`（补偿恢复原版本），响应必须原样返回。`effective_at` 为第三方实际完成该方向版本切换的首次生效时间，必须是带 UTC 时区的 ISO 8601 时间（`Z` 或 `+00:00`），不能使用重试时间或平台请求时间。

CHANGE 必须携带幂等键，可使用请求体 `idempotency_key` 或请求头 `X-Request-Id`；两者同时存在时必须一致。相同幂等键和相同 payload 必须始终返回首次成功结果及首次 `effective_at`，包括应用重启后或发生后续切换后重试；相同键但不同 payload 返回 HTTP 409，不得再次切换。target 和 original 使用不同幂等键。

同一产品内的同一 `change_id + direction` 必须始终使用首次绑定的幂等键，即使重启或发生后续版本切换也不得更换。更换键返回 HTTP 409，不得再次切换或重写 `effective_at`；该身份与幂等结果必须持久化，并与版本状态在同一事务内提交。

### 3.5 状态及最后版本切换回执 `STATUS`

```json
{"type":"STATUS","tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","admin_user_id":"platform-admin-user-id","oidc_client_id":"product-oidc-client-id","change_id":"change-10001","direction":"target"}
```

版本切换后的完整成功响应示例（以下字段均必须返回，必须位于业务响应根节点）：

```json
{"ret":0,"tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","version":"professional","status":"active","admin_user_id":"platform-admin-user-id","admin_active":true,"application_ready":true,"oidc_client_id":"product-oidc-client-id","expires_at":"2027-10-15T08:00:00+00:00","change_id":"change-10001","direction":"target","effective_at":"2026-10-15T08:00:00.123456+00:00","refund_shorten_protocol":"refund_shorten_v1","refund_shorten_supported":true,"refund_id":null}
```

STATUS 请求中的 `tenant_id/product_id/enterprise_id/subscription_id/admin_user_id/oidc_client_id` 必须与 OPEN、管理员同步及平台登记的配置一致；版本切换验证还发送 `change_id/direction`。响应必须反映第三方实际状态，不得仅原样回显请求。平台严格核对上述六项身份、目标 `version`、`status="active"`、布尔值 `admin_active=true` 和 `application_ready=true`；字符串 `"true"` 不合格。管理员未激活、应用未就绪或客户端不匹配时必须如实返回失败状态，平台将拒绝自动验证。

`expires_at` 字段必须返回，表示 OPEN、最近一次普通 RENEW 或下述已确认退款缩期后的真实订阅到期时间，使用带时区的 ISO 8601 时间；必须与平台冻结的订阅到期时刻一致，CHANGE 不得延长或缩短它。只有平台订阅本身无到期时间时才允许返回 `null`，不能用缺字段或 `null` 替代未知到期时间。缺少任何必需证明字段均不能完成版本切换验证。

STATUS 必须返回最后一次实际版本切换的 `change_id/direction/effective_at`。尚未发生版本切换时三项为 `null`；OPEN、RENEW、CLOSE、用户及部门操作均不得覆盖版本回执。重放旧 CHANGE 只返回旧结果，不得将 STATUS 的最后回执回退。无法提供可信回执的应用走人工核定，不能将缺失时间视为零消费或据此宣称自动退款执行。平台统一 OAuth/OIDC 登录与机器鉴权协议保持不变。

### 3.6 退款尾部缩期 `refund_shorten_v1`

普通 RENEW 只允许续期，不得缩短服务时间。退款缩期是独立的显式能力：支持方必须在 STATUS 根节点返回 `refund_shorten_protocol="refund_shorten_v1"`、布尔值 `refund_shorten_supported=true` 及最后成功缩期的 `refund_id`（尚无缩期为 `null`）。不支持方须如实返回 false 或不广告此能力，平台拒绝自动回收，转人工核定；不得把普通续期成功视为退款回收成功。

平台准备与执行时查询示例（版本切换 STATUS 所需的管理员及 OIDC 验证字段仍按 3.5 返回；缩期查询不要求在请求中提供管理员字段）：

```json
{"type":"STATUS","tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","action":"refund_shorten","refund_id":"refund-10001","protocol":"refund_shorten_v1"}
```

缩期前回读的关键根节点字段（其余 STATUS 字段沿用 3.5）：

```json
{"ret":0,"tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","expires_at":"2027-10-15T08:00:00.654321+00:00","refund_shorten_protocol":"refund_shorten_v1","refund_shorten_supported":true,"refund_id":null}
```

执行请求：

```json
{"type":"RENEW","action":"refund_shorten","protocol":"refund_shorten_v1","tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","refund_id":"refund-10001","expected_expires_at":"2027-10-15T08:00:00.654321+00:00","expires_at":"2027-09-15T08:00:00.530865+00:00","idempotency_key":"refund-shorten:refund-10001"}
```

必须完整校验产品、租户、企业及订阅归属。在同一持久事务内，将实际当前到期时间与 `expected_expires_at` 按 UTC 时刻精确比较（CAS），一致才允许改为 `expires_at`；不一致返回 HTTP 409，不能覆盖并发续期或其它退款。新时间必须严格早于原时间且尚未经过；不能延长、不能消费已过去的尾部。两个时间必须为 UTC ISO 8601（`Z` 或 `+00:00`），精度保留至微秒，最多六位小数，不可取整为秒或毫秒。未知、无时区、非 UTC、超过微秒精度或永久订阅的空到期时间不得自动缩期。

成功响应：

```json
{"ret":0,"tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","refund_id":"refund-10001","protocol":"refund_shorten_v1","expected_expires_at":"2027-10-15T08:00:00.654321+00:00","expires_at":"2027-09-15T08:00:00.530865+00:00","refund_shorten_protocol":"refund_shorten_v1","refund_shorten_supported":true,"idempotency_key":"refund-shorten:refund-10001"}
```

同一产品内每个 `refund_id` 只绑定一次缩期结果，必须固定使用 `refund-shorten:{refund_id}`。相同键与相同完整 payload（包括 CAS 原时间及新时间）重试，跨进程重启也返回首次结果，不再次应用 CAS 或缩期；同键不同 payload 返回 HTTP 409，更换固定键拒绝执行。退款身份、幂等结果及到期时间变更必须原子持久化。STATUS 必须返回实际最新到期时间与最后成功缩期的 `refund_id`，不得回显查询中未执行的退款 ID；重放旧请求不回退最新状态。

退款缩期不得覆盖 `change_id/direction/effective_at` 版本回执；普通续期、用户和部门操作也不得覆盖最后缩期回执。第三方应用必须在 SSO 回调及每次现有会话访问业务页面时校验最新租户到期时间，到期立即拒绝访问，不能仅等待旧登录会话过期。

本轮只支持可用单一租户到期时间表达的订阅末尾回收。若存在后续无关续费段，平台资源适配器会拒绝中间段回收，不宣称支持保留后段却删除中间段。缩期成功仅证明资源期限回收；BE012 资金执行为本地模拟记录，不代表银行到账或 BE013 清算接入完成。

## 4. 用户和部门接口

### 4.1 增加或恢复用户 `USER_ASSIGN`

```json
{"type":"USER_ASSIGN","tenant_id":"tenant-10001","user_id":"platform-user-id","username":"user@example.com","name":"张三","email":"user@example.com","phone":"13800000000","department_id":"external-department-1","department_ids":["external-department-1","external-department-2"]}
```

`department_ids` 为可选字段，表示用户所属的多个 SaaS 部门；平台会将企业部门 ID 映射为第三方部门 ID 后传入。兼容只处理 `department_id` 的旧接入时，平台仍会同时发送首个部门到 `department_id`。

同步管理发送当前已同步部门的第三方 ID；未同步部门不包含在数组内。再次添加已映射用户时，平台重新计算部门列表并调用 `USER_ASSIGN` 更新关联；空数组表示清除部门关联。第三方应以本次列表替换原关联，而非仅追加。相同状态的重复请求复用成功结果；删除后恢复、部门列表改变后再恢复旧列表会发起新的实际调用。

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
{"type":"DEPT_CREATE","tenant_id":"tenant-10001","department_id":"platform-saas-department-mapping-id","parent_id":"","name":"生产部"}
```

成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","department_id":"external-department-1","status":"active"}
```

### 4.4 删除部门 `DEPT_REMOVE`

```json
{"type":"DEPT_REMOVE","tenant_id":"tenant-10001","department_id":"external-department-1"}
```

同步管理按父部门在先创建、子部门在先删除的顺序执行。仍有关联成员或已同步子部门的部门会被阻止删除并报告单项失败；需先移除成员关联及子部门，再移除父部门。平台保留最近一次成功同步的多部门关联，即使平台成员已离职或部门已停用，也不会绕过关联检查。第三方应返回 `status=deleted` 或 `status=disabled`，不得因重复删除返回不可解析错误。

### 4.5 平台同步管理约束

仅购买企业已实名、已激活且启用的超级管理员或管理员可使用同步管理；平台账号不可管理或添加到同步列表。企业须存在且已实名，订阅须处于有效期内的 `active` 状态。企业模型当前没有独立启停字段。

平台管理接口 `sync-actions` 使用 `action=add|remove`、`kind=users|departments`，并且只能提供非空 `ids` 或 `all=true` 之一。每次最多处理 10 项，全部操作超过 10 项也会拒绝，需分批选择。右侧保留不可用成员、停用或已删除平台部门的映射，便于移除；用户可显示在多个部门，实际操作按用户去重。所有移除操作均需界面确认。

平台拒绝移除成功交付记录标记的 SaaS 主管理员，并阻止移除最后一个已同步且有效的企业管理员。操作使用实际第三方调用和操作台账，最多尝试 3 次，失败后间隔 10 秒再试；HTTP 超时使用应用接口配置。返回逐项成功、失败或跳过结果，并记录状态前后及失败原因。单项失败不视为整批成功。

## 5. 生命周期闭环

1. 产品登记生成 `product_id`，配置第三方地址和 OAuth2 凭据。
2. 企业购买某个版本，平台生成 `subscription_id`，调用 `OPEN`。
3. 平台保存第三方 `tenant_id/app_id`，并按需调用部门和用户同步接口。
4. 续费调用 `RENEW`，到期时间顺延。
5. 升级先生成差价订单，支付确认后调用 `CHANGE`；降级调用 `CHANGE` 并留存切换回执及退款核定记录，不代表自动执行退款；无可信回执时人工核定。
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

本节用于用户浏览器登录，不能替代机器接口认证。SaaS 作为 OIDC 客户端，平台作为 OP，采用授权码 + PKCE S256。SaaS 配置 `oidc_sso` 凭据和 `resource_server` 校验信息；平台内部机器客户端不需要由 SaaS 配置，禁止把机器 Token 当作用户身份。

### 8.1 登记与客户端配置

产品登记时填写 SaaS 应用访问地址和精确 OIDC 回调地址。四阶段审核通过后平台自动创建独立用户登录客户端，无需提供方手工调用创建接口。提供方企业超级管理员、管理员通过第 1.2 节统一下载文件的 `oidc_sso` 分区获得配置，可重复下载，不轮换密钥。`POST /api/products/{product_id}/oidc-client` 仅作为受控兼容配置接口；其创建时的临时返回不替代统一下载功能。客户端密钥必须由服务端安全保存，浏览器不得保存 Secret 或 OIDC Token。

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
