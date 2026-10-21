# SaaS 应用第三方接口对接规范

版本：v1.6；接入配置 JSON 格式：2.0；订单对账上下文格式：1。

本文件是第三方SaaS的独立接入开发和联调规范，以当前平台实现为准。所有示例ID、域名、Token和Secret均为占位符，不可直接用于正式接入。除本规范外，提供方还必须取得该产品的真实接入配置文件及可达环境地址；不需要阅读平台源码或另一本内部规范来推断协议。

## 0. 实施顺序与责任边界

| 步骤 | SaaS提供方需要完成 | 平台负责 |
| --- | --- | --- |
| 1 | 准备稳定应用入口、精确回调URL、管理接口URL及 `/health` | 登记产品和版本，执行四级审核 |
| 2 | 下载统一接入配置，将OIDC Secret保存在服务端 | 审核后生成产品级OIDC及内部机器OAuth客户端 |
| 3 | 实现机器Token内省、Scope与租户身份校验 | 用内部机器凭据向平台自身获取Token |
| 4 | 实现本文件全部管理操作及幂等持久化 | 主动调用SaaS，记录请求、重试及执行结果 |
| 5 | 实现OIDC授权码+PKCE、回调、身份映射和本地会话 | 校验购买权益、成员授权，签发用户登录令牌 |
| 6 | 提供测试环境，执行第7节测试矩阵 | 发起测试订单、模拟支付、续费、变更及退款 |
| 7 | 核对订单时段、回执与第三方实际租户期限 | 提供第9节的只读权益查询 |

必须区分三类凭证：平台人员登录Bearer用于下载/控制面；机器OAuth Token用于平台调用SaaS管理接口；OIDC用户Token用于SaaS识别登录用户。三者不能互换。买方企业的用户共享产品级OIDC客户端配置，但绝不共享个人身份或租户会话。

当前一个企业与一个SaaS产品只维护一个订阅组合，重复购买同版本续费现有组合；不要按每张续费订单另建租户。首次支付后先准备租户和管理员，再用RENEW激活冻结时段；后续续费复用该租户。

阅读路径：凭据与OAuth见第1节；公共约定见第2节；管理接口见第3、4节；端到端顺序见第5节；接口责任见第6节；测试见第7节；完整OIDC见第8节；时间及对账见第9节；持久化、排障和边界见第10节。

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

OIDC使用授权码流程和PKCE S256，openid必需，profile/email可选，示例使用 `openid profile email`；SaaS必须核验state、nonce、ID Token签名及issuer/audience，并按企业和租户隔离会话。不能把client_credentials令牌当作用户登录令牌。

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

### 1.4 稳定的应用入口与产品上下文

登记的 `application_url` 应是 SaaS 的稳定业务入口，不应要求提供方在 OIDC 客户端生成后，重新把客户端ID拼入业务入口。SaaS 服务端根据产品配置定位其 OIDC 客户端，再发起授权码及PKCE登录。

平台启动应用时，会替换应用URL中已有的 `product_id` 和 `enterprise_id` 参数，填入当前产品及购买企业的真实标识，保留其他非冲突参数。示例：

```text
https://saas.example/apps/production?product_id=platform-product-id&enterprise_id=buyer-enterprise-id
```

这两个标识是上下文，不是用户授权凭证。SaaS 必须在后续 OIDC 登录中验证令牌声明与当前产品、企业、订阅、租户一致；不能只凭URL中的企业ID直接登录或创建会话。入口URL不得包含客户端密钥或访问Token。OIDC协议的授权请求仍会按协议传递公开的 `client_id`，这与业务入口要求无关。

开发模拟 SaaS 支持两类稳定入口：

- `/apps/{product_id}?enterprise_id=...`：按公开产品标识定位已配置客户端；推荐把不带查询参数的 `/apps/{product_id}` 登记为产品应用访问地址。
- `/sso/apps?product_id=...&enterprise_id=...`：共用入口，适用于登记时暂未取得产品ID的情况；平台会在启动时补入产品和企业上下文。

管理页显示可复制的产品稳定入口。未提供企业上下文时，稳定入口只显示公开入口页，不建立用户会话。缺少产品配置返回404，同一产品有多个无法唯一选择的客户端时返回409，不默认选第一条。历史 `/sso/apps/{client_id}` 保留兼容，但新应用不应依赖该路径。

平台在发布接入验证及自动交付完成前，检查应用入口的路径可达性：使用无认证的HEAD，超时5秒，HEAD不支持（405／501）时使用有界GET；接受2xx或3xx但不跟随跳转。不发送机器Token、OIDC密钥或用户凭据。探测带公开的 `X-Market-Product-Id` 上下文，SaaS无需把它当作认证凭据；依赖产品选择的入口可用它提供不改变业务状态的HEAD响应。

当前安全探测针对登记地址的协议、主机及路径，不执行查询参数驱动的业务或浏览器登录。第三方应让稳定路径支持上述公开HEAD检查；查询参数、重定向目标及最终登录是否正常，仍须通过真实浏览器SSO联调确认。错误路径、超时或拒绝结果不能作为交付成功；内部私网入口需由平台显式授权精确主机，不能为通过检查而关闭SSRF保护。

### 1.5 OAuth2机器调用完整流程

```text
平台业务进程       平台OAuth授权服务器          SaaS管理接口
    |--内部client_credentials-->|
    |<--机器access_token--------|
    |---------------POST 操作 + Bearer-------------->|
    |                          |<--POST introspect---|
    |                          |---active及claims--->|
    |                          |        SaaS校验Scope/产品/租户/幂等
    |<--------------执行结果及持久化回执-------------|
```

1. 平台保存产品级内部机器client_id/client_secret，在自身 `/oauth/token` 使用form-urlencoded请求，grant_type为client_credentials，scope为获准的 `saas.tenant saas.user saas.department`。这一步只由平台执行，SaaS提供方既不配置另一授权服务器，也不需要获取这组Secret。
2. 平台拿到 `access_token/token_type=Bearer/expires_in/scope`，当前机器有效期3600秒。Token携带iss、aud、client_id、product_id、scope、iat、exp、jti，但不包含当前购买方用户、企业、订单或租户的业务授权，不能据Token自行推断买方身份。
3. 平台以Bearer调用SaaS操作地址。SaaS从Authorization提取Token，通过下载配置指定的内省端点POST表单token。禁止把Token写入业务URL、访问日志或错误页面。
4. 平台内省校验签名、期限、issuer、audience、客户端启用状态、产品归属、Scope以及单Token撤销记录；未知/过期/撤销返回 `active=false`。SaaS只接受布尔 `active=true`，并校验iss/aud/product_id/Scope，不把字符串 `"true"` 当真。
5. SaaS以本地产品绑定找到租户，再验证enterprise_id/subscription_id及订单上下文。用户/部门旧兼容请求可能只在上下文中提供归属，须与持久化租户关系核对，不可凭任意tenant_id越权。
6. SaaS在事务内持久化业务变化和幂等响应，再回传根节点业务结果。网络超时不能使相同幂等键重复生效。

建议SaaS每次管理调用内省；当前模拟服务如此实现。若自行缓存内省结果，撤销生效会延迟，须另外协商最大延迟并测试，不能假设JWT未过期就仍被允许。内省网络错误应返回503或明确服务失败，不得失败放行。当前内省未要求额外客户端认证，不代表已实现生产级资源服务器认证；对外正式部署必须评估TLS、入口限制、限流和认证加固。

机器Token撤销与产品状态不是一回事。单Token撤销、机器客户端停用均可使内省失败；手工下架不等于自动撤销已售权益，安全下架按平台资源策略处理。SaaS仍以最新内省与具体租户业务状态判断，不自行把所有非published状态当作全部租户关闭。

## 2. 业务接口公共约定

```http
POST {base_url}/isv.php
Authorization: Bearer {access_token}
Content-Type: application/json
Accept: application/json
X-Request-Id: {optional_operation_id}
```

平台发送 JSON 请求，所有请求带 `type` 字段，第三方据此分派操作。当前通用调用主要通过 JSON 的 `idempotency_key` 传递幂等键，不能要求所有请求必有 `X-Request-Id`。若收到该可选请求头，应与 JSON 幂等键一致，否则拒绝；第三方须记录产品范围内的幂等键并防止同键不同请求重复执行。

首次交付、版本变更及退款缩期会显式传根节点idempotency_key。部分兼容/同步请求只带 `reconciliation_context.event_id` 而没有根节点幂等键；第三方不得因此拒绝所有旧请求，可将该稳定event_id用于操作去重并保留业务映射。纯STATUS不得因重复查询而产生写入。不建议自行要求平台当前未承诺发送的额外字段或请求头。

成功响应：

```json
{"ret":0,"msg":"ok","request_id":"provider-request-id","data":{}}
```

`data`仅示意附加业务内容。平台核验所需的 `tenant_id`、`app_id`、`status`及各操作身份字段必须按后文放在根节点。失败响应：

```json
{"ret":1,"msg":"tenant already exists","code":"TENANT_EXISTS","request_id":"provider-request-id","retryable":false}
```

HTTP 401 表示 Token 无效，403表示权限不足，400/422表示参数错误，404表示资源不存在，409表示身份、幂等或状态冲突，429表示限流，5xx表示服务异常。成功应使用HTTP 200及整数 `ret=0`。失败可以返回非2xx和 `detail`，或业务失败 `ret!=0` 与 `msg`；不得用HTTP 200加缺失证明字段伪装执行成功。

下面是本平台实际兼容边界，而非对所有OAuth服务的通用声明：

- 新版自动交付和版本/退款资源校验要求关键业务字段位于响应根节点，不能只放入 `data`。`ret` 必须为整数0，不是字符串或布尔值。上面的 `data` 只可存放附加信息，不替代根节点字段。
- `retryable`、`Retry-After` 可辅助排障，但当前适配器没有按这些字段建立通用分类调度。通用SaaS调用会将HTTP/业务失败转为平台502；自动交付当前每轮最多3次失败尝试、失败间隔10秒，正常分阶段推进不算失败重试。旧同步操作也最多尝试3次；版本和退款由各自Saga恢复，不能假定所有路径都是同一重试节奏。
- HTTP 401使平台清除旧机器Token缓存，后续业务重试重新获取，不在该次请求内立即重发。第三方必须为超时后实际已成功、平台尚未收到响应的情况提供持久化幂等回执。
- 超时取平台产品接口配置 `timeout_ms`，不是固定30秒。正式接入需双方确认超时及网络访问策略，不能靠扩大超时替代幂等。

### 2.1 操作与Scope映射

| 操作 | 必须包含的机器Scope |
| --- | --- |
| OPEN、CLOSE、RENEW、CHANGE、STATUS | `saas.tenant` |
| USER_ASSIGN、USER_UNASSIGN | `saas.user` |
| DEPT_CREATE、DEPT_REMOVE | `saas.department` |

Scope匹配按空格分隔后的完整值判断，不作字符串子串匹配。通过Scope仅证明操作类别被授权，还须验证Token的产品、请求中的企业/订阅和实际租户归属。

## 3. 租户接口

### 3.1 开通租户 `OPEN`

请求：

```json
{"type":"OPEN","enterprise_id":"platform-enterprise-id","product_id":"platform-product-id","subscription_id":"platform-subscription-id","version":"professional","admin_user_id":"platform-admin-user-id","oidc_client_id":"product-oidc-client-id","idempotency_key":"delivery:platform-subscription-id:order-1:OPEN"}
```

成功至少返回：

```json
{"ret":0,"tenant_id":"tenant-10001","app_id":"app-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","version":"professional","status":"opened"}
```

平台保存 `tenant_id` 和可选 `app_id`，后续租户操作使用 `tenant_id`。同企业同产品后续续费复用该租户，不创建独立租户。`tenant_id` 必须为非空稳定字符串，最长180字符，不能用本次请求ID替代。

上述示例是新版OPEN核心请求；新冻结请求还包含第9节的完整 `reconciliation_context`，其中订单号可追踪、期限为provisional/null。OPEN本身不宣称交付完成，也不确定最终购买起止时间，不要求企业名称、contact或billing_cycle必有值。第三方须保存产品、企业、订阅、版本及管理员/OIDC关联；`oidc_client_id` 使用下载文件中的 `oidc_sso.client_id`。管理员是平台用户ID，不是第三方用户ID。允许已准备的租户继续USER_ASSIGN及STATUS，但不能因期限尚未激活就开放普通业务。完成后续RENEW和完整STATUS核验才视为交付成功。

### 3.2 关闭租户 `CLOSE`

此为保留的受控兼容管理能力，不是新版订单页面的常规操作按钮；第三方无需自行发起平台CLOSE。关闭与订单退款、Token撤销不是同一个动作。

```json
{"type":"CLOSE","tenant_id":"tenant-10001","subscription_id":"platform-subscription-id"}
```

第三方关闭后至少保留租户和业务数据 1 个月，并返回：

```json
{"ret":0,"tenant_id":"tenant-10001","status":"closed","recover_until":"2026-11-05T00:00:00Z"}
```

### 3.3 续费 `RENEW`

本接口同时用于首次交付激活和已付续费订单延期；不用于由第三方决定新到期日。**平台先计算并冻结时段，再请求第三方执行；响应只是执行回执，不能作为平台计算续费截止日期的数据来源。**

完整新版请求示例（续费3个月；第9节说明上下文语义）：

```json
{
  "type": "RENEW",
  "tenant_id": "tenant-10001",
  "product_id": "platform-product-id",
  "enterprise_id": "platform-enterprise-id",
  "subscription_id": "platform-subscription-id",
  "version": "professional",
  "subscription_months": 3,
  "starts_at": "2026-11-19T04:00:00+00:00",
  "expires_at": "2027-02-19T04:00:00+00:00",
  "platform_delivery_at": "2026-10-19T04:00:00+00:00",
  "admin_user_id": "platform-admin-user-id",
  "oidc_client_id": "product-oidc-client-id",
  "idempotency_key": "delivery:platform-subscription-id:order-renew-1:RENEW",
  "reconciliation_context": {
    "schema_version": 1,
    "event_id": "delivery:platform-subscription-id:order-renew-1:RENEW",
    "event_type": "RENEW",
    "product_id": "platform-product-id",
    "enterprise_id": "platform-enterprise-id",
    "subscription_id": "platform-subscription-id",
    "tenant_id": "tenant-10001",
    "order_id": "order-renew-1",
    "order_no": "ORD-example-renew",
    "order_starts_at": "2026-11-19T04:00:00+00:00",
    "order_ends_at": "2027-02-19T04:00:00+00:00",
    "subscription_expires_at": "2027-02-19T04:00:00+00:00",
    "effective_at": "2026-10-19T04:00:00+00:00",
    "effective_at_semantics": "platform_delivery",
    "period_status": "confirmed",
    "affected_orders": []
  }
}
```

| 字段 | 必需性与含义 |
| --- | --- |
| tenant_id/product_id/enterprise_id/subscription_id | 新版必传，须匹配已开通租户与当前机器Token产品 |
| version | 新版必传，当前购买的商品版本，不从OIDC客户端名推断 |
| subscription_months | 新版必传，1～36整数，是本次续费月数，不是累计月数 |
| starts_at | 新版必传，本次购买段开始，不是请求发生时间 |
| expires_at | 新版必传，平台冻结的本次续费段结束及续费后的租户截止时间 |
| platform_delivery_at | 新版必传，平台冻结激活计划时刻，提前续费时可早于starts_at |
| admin_user_id/oidc_client_id | 新版必传，原管理员与该产品OIDC关联 |
| idempotency_key | 新版必传，本订单本操作的稳定键 |
| reconciliation_context | 新冻结请求必传；订单ID/订单号位于此对象，不是根节点 |

有效期内从现有已交付组合末尾顺延；已到期重新购买从本次实际交付激活锚点计算。按平台保留的购买日历锚点加月份，目标月份无对应日期取月末，第三方不要逐次按30天或基于截断后的月末重新推算。例：1月31日锚点，第1月边界为2月末、第2月边界为3月31日。提前续费不重置当前配额，也不提前启用未来版本。

第三方在同一事务内校验归属及幂等，将最终截止保存为传入的 `expires_at`，不是在本地当前截止上再次累加月份。先查幂等结果，再判断当前状态；成功后即使发生后续续费，同键重试仍返回首次结果而不回退最新截止。相同键不同完整请求返回409。普通续费不能缩短现有期限；首次激活允许原截止尚未设置。

成功响应的最小业务字段：

```json
{"ret":0,"tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","status":"active","starts_at":"2026-11-19T04:00:00+00:00","expires_at":"2027-02-19T04:00:00+00:00","platform_delivery_at":"2026-10-19T04:00:00+00:00"}
```

建议同时保存并返回第9节的上下文和首次受理回执。平台随后查询STATUS，以已保存的冻结请求为基准核对租户身份、管理员、版本、应用就绪和实际截止时间；不一致不能确认交付完成。时间请按平台传入的UTC ISO格式保存/返回，保留小数精度，避免当前激活校验中的字符串比较因格式不同失败。

**普通RENEW当前不发送 `expected_expires_at`，第三方不能把它设成必填；该字段仅用于第3.6节退款缩期的CAS校验。** `billing_cycle` 为历史入口兼容字段，新版不保证发送，第三方不得仅据其重新计算到期时间。历史已冻结请求可能没有完整上下文，不得为重放补写字段或更换幂等键。

### 3.4 版本变更 `CHANGE`

升级或降级请求：

```json
{"type":"CHANGE","tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","to_version":"professional","admin_user_id":"platform-admin-user-id","oidc_client_id":"product-oidc-client-id","change_id":"change-10001","direction":"target","idempotency_key":"version-change:change-10001:target"}
```

升级在平台差价订单支付确认后调用；降级切换及退款核定分别留证，不表示自动执行退款。成功返回：

新版请求还带第9节上下文，截止位于 `reconciliation_context.subscription_expires_at`；`affected_orders`包含当前及未来已购续费段。`from_version/change_type/billing_cycle`并非新版必传字段，不能强制要求。第三方只执行to_version，不自行计算买方差价或清算；补偿direction=original也按传入to_version恢复。

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

当前自动缩期只支持可用单一租户到期时间表达的订阅末尾回收。若存在后续续费，须先对后续续费订单退款，再对较早订单退款；平台拒绝直接挖除中间段。缩期成功仅证明资源期限回收，不代表买方资金或清算收付已完成；第三方不根据该回执自行退款。当前支付/退款联调使用平台模拟资金流程，不代表银行已接入。

## 4. 用户和部门接口

### 4.1 增加或恢复用户 `USER_ASSIGN`

```json
{"type":"USER_ASSIGN","tenant_id":"tenant-10001","user_id":"platform-user-id","name":"张三","email":"user@example.com","department_id":"external-department-1","department_ids":["external-department-1","external-department-2"]}
```

`department_ids` 为可选字段，表示用户所属的多个 SaaS 部门；平台会将企业部门 ID 映射为第三方部门 ID 后传入。兼容只处理 `department_id` 的旧接入时，平台仍会同时发送首个部门到 `department_id`。

同步管理发送当前已同步部门的第三方 ID；未同步部门不包含在数组内。再次添加已映射用户时，平台重新计算部门列表并调用 `USER_ASSIGN` 更新关联；空数组表示清除部门关联。第三方应以本次列表替换原关联，而非仅追加。相同状态的重复请求复用成功结果；删除后恢复、部门列表改变后再恢复旧列表会发起新的实际调用。

删除后的用户再次调用该接口即可恢复。成功返回：

```json
{"ret":0,"tenant_id":"tenant-10001","product_id":"platform-product-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","platform_user_id":"platform-user-id","user_id":"external-user-1","external_user_id":"external-user-1","status":"active"}
```

`external_user_id` 必须返回稳定、非空的第三方用户ID，新版首次交付会同时检查它与 `user_id` 一致。`platform_user_id`保留原平台用户ID。不要仅按邮箱合并用户，不同租户中的相同用户或相似ID前缀不能冲突；恢复同一租户下的用户应复用原映射。

首次交付同步主管理员时，请求还包含 `product_id/enterprise_id/subscription_id/admin_user_id/oidc_client_id`；name/email/department_ids并非该路径必传。SaaS必须把该平台用户识别为指定租户的主管理员，并允许STATUS反映其激活状态。普通同步不保证携带admin_user_id或oidc_client_id，不能要求所有USER_ASSIGN都包含它们。第9节上下文提供产品及订阅归属关联；不要把普通员工自动提升成管理员。

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

删除响应示意：

```json
{"ret":0,"tenant_id":"tenant-10001","department_id":"external-department-1","status":"deleted"}
```

### 4.5 平台同步管理约束

仅购买企业已实名、已激活且启用的超级管理员或管理员可使用同步管理；平台账号不可管理或添加到同步列表。企业须存在且已实名，订阅须处于有效期内的 `active` 状态。企业模型当前没有独立启停字段。

平台管理接口 `sync-actions` 使用 `action=add|remove`、`kind=users|departments`，并且只能提供非空 `ids` 或 `all=true` 之一。每次最多处理 10 项，全部操作超过 10 项也会拒绝，需分批选择。右侧保留不可用成员、停用或已删除平台部门的映射，便于移除；用户可显示在多个部门，实际操作按用户去重。所有移除操作均需界面确认。

平台拒绝移除成功交付记录标记的 SaaS 主管理员，并阻止移除最后一个已同步且有效的企业管理员。操作使用实际第三方调用和操作台账，最多尝试 3 次，失败后间隔 10 秒再试；HTTP 超时使用应用接口配置。返回逐项成功、失败或跳过结果，并记录状态前后及失败原因。单项失败不视为整批成功。

## 5. 生命周期闭环

1. 登记产品版本、应用入口、管理接口及OIDC回调；不由提供方填写平台内部机器Secret。
2. 完成业务、质量、安全和运营审核，进入待接入验证，提供方下载并配置统一接入文件。
3. 执行管理 `/health`、应用入口及OIDC登记检查，成功才正式可购买；这一步不证明浏览器登录已通过。
4. 买方企业超级管理员或管理员下单，企业超级管理员发起支付，平台按权限完成模拟/人工到账确认。未支付不自动开通。
5. 首次交付依次执行OPEN、主管理员USER_ASSIGN、预检STATUS、冻结RENEW激活、最终STATUS以及入口可达检查。任一必要证明失败不算履约完成；失败重试复用已有操作结果，避免重复租户。
6. 最终核验成功后平台保存订单服务时段和订阅截止，标记交付完成；买方“我的应用”或订单入口才用于实际SSO访问。
7. 同版本已付续费复用租户，按RENEW中的冻结起止日期延期；企业主管理员不变时可复用已验证映射，不保证每次续费都重新OPEN/USER_ASSIGN。
8. 升级以差价支付及资源变更成功为条件，降级与退款补偿由平台Saga关联；SaaS仅执行CHANGE并提供真实回执，不自行决定资金成功。平台分别处理原版本冲回和新版本清算。
9. 普通退款经平台审批执行后，以显式退款缩期RENEW回收权益，回读STATUS验证新期限；失败不靠普通续费来覆盖。
10. 获授权普通成员通过OIDC访问；部门与成员同步按第4节执行。平台订单页不再提供常规“关闭租户/恢复租户”按钮，CLOSE/恢复仅保留受控兼容能力。

## 6. 接口清单及调用责任

### 6.1 SaaS必须实现，平台调用

| HTTP与地址 | 操作 | 认证及用途 |
| --- | --- | --- |
| POST 已登记的管理操作地址，默认 `/isv.php` | OPEN、RENEW、CHANGE、STATUS及第4节操作；CLOSE为兼容能力 | 机器Bearer，按type分派 |
| GET 管理地址同协议/主机/端口的 `/health` | 接入健康验证 | 平台发送机器Bearer；5秒；只接受2xx，不跟随跳转，不解析响应体 |
| HEAD 稳定应用入口；不支持时有界GET | 入口路径可达探测 | 无认证，公开X-Market-Product-Id，2xx/3xx，不跟随跳转，无状态写入 |
| GET 已登记的OIDC回调 | 处理授权码或拒绝 | 浏览器携带code/state或error/state；须校验原事务，不信任URL本身 |

管理健康URL不是 `base_url + operation_path + /health`：例如管理接口为 `https://saas.example/api/isv.php`，健康探测仍访问 `https://saas.example/health`。建议健康接口验证机器Bearer及产品，但不创建租户。应用入口HEAD应无需先登录，不应与需鉴权的管理健康接口混为一谈。

### 6.2 平台实现，SaaS服务端调用

| HTTP与地址 | 用途 |
| --- | --- |
| POST 下载文件中的resource_server.introspection_url | 机器Token内省；表单token |
| GET oidc_sso.discovery_url / jwks_url | 获取受信任发现信息及公钥 |
| POST oidc_sso.token_url | 服务端用授权码+PKCE及OIDC客户端认证换用户Token |
| GET oidc_sso.userinfo_url | 用户访问令牌查询身份及当前购买权限 |

授权地址由浏览器访问，不是用机器Token调用的租户创建接口。所有地址以该产品下载文件为准，不把 `/api` 自动拼到OAuth/OIDC路径前。

### 6.3 平台控制面，仅被授权人员使用

| 方法与路径 | 用途及边界 |
| --- | --- |
| GET `/api/products/{product_id}/saas-integration-credentials-download` | 提供方管理员下载统一配置；不返回内部机器Secret |
| POST `/api/products/{product_id}/saas-integration/verify` | 无请求体；提供方已激活已实名企业管理员执行，当前拒绝平台角色账号；产品须待接入验证 |
| GET/PUT `/api/products/{product_id}/saas-integration` | 受控配置，不是SaaS侧需要实现的接口 |
| GET/POST `/api/products/{product_id}/saas-versions` | 受控商品版本管理 |
| POST `/api/orders/{order_id}/renewal` | 新版订单续费，生成未支付订单，不等于直接调用SaaS RENEW |
| POST `/api/orders/{order_id}/version-changes` | 新版版本变更Saga入口，资金/资源分阶段执行 |
| GET `/api/saas-subscriptions/{id}/sync-tree`；POST 同前缀 `/sync-actions` | 购买方管理员成员/部门管理 |
| GET `/api/saas-subscriptions/{id}/operations` | 操作台账 |
| GET `/api/provider/products/{product_id}/entitlements` | 提供方权益对账，详见第9节 |

以上接口使用平台人员登录Bearer，不是机器Token或OIDC用户Token。历史 `/api/products/{id}/saas-subscriptions`、`/api/saas-subscriptions/{id}/renew`、`/change-version`及close/restore等兼容入口不作为新版交易入口；组合托管订阅不允许绕过订单Saga。第三方SaaS无需调用它们来主动完成收款或改价。

## 7. 联调步骤与测试矩阵

先在独立测试产品及企业完成下表。机器Token由平台发起测试订单或受控运维联调取得；第三方不能用OIDC Secret调用 `/oauth/token`。命令中的Token仅从临时环境变量读取，不粘贴进文档或提交仓库。

```bash
# SaaS服务端验证已收到的机器Bearer，正常响应active=true。
curl --fail-with-body --request POST "$INTROSPECTION_URL" \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "token=$RECEIVED_MACHINE_TOKEN"

# 获取发现信息；issuer须与接入配置完全一致。
curl --fail-with-body "$OIDC_DISCOVERY_URL"

# 平台侧受控联调调用SaaS；payload.json采用第3/4节完整业务请求。
curl --fail-with-body --request POST "$SAAS_OPERATION_URL" \
  --header "Authorization: Bearer $PLATFORM_TEST_MACHINE_TOKEN" \
  --header 'Content-Type: application/json' --data-binary @payload.json
```

| 编号 | 场景与操作 | 预期及证据 |
| --- | --- | --- |
| T01 | 下载配置、检查外网/专网DNS、TLS、issuer及所有端点可达 | Secret仅服务端；地址不能要求第三方解析market-api内部DNS |
| T02 | 有效、过期、撤销及其他产品机器Token访问管理接口 | 有效才执行；无效401、Scope/产品不符403；内省不可达不执行 |
| T03 | GET管理/health、HEAD应用入口及错误路径 | 健康2xx；入口公开可达；404/超时阻止成功，不虚报浏览器SSO已通过 |
| T04 | 首购支付触发完整分阶段交付 | 唯一租户、正确管理员、RENEW冻结期限与STATUS一致，最后交付完成 |
| T05 | 有效期内提前续费3个月 | starts_at为旧末尾，截止采用传入值；同租户、不提前重置权益 |
| T06 | 月末及闰年日期、到期重新购买 | 采用平台日历边界；不得固定30天或累积2月截断误差 |
| T07 | 同键同请求重复、超时后重放、SaaS进程重启后重放 | 返回首次结果，不重复延长；同键改字段409，不改变最新状态 |
| T08 | 升级、降级及补偿恢复原版本 | 方向独立幂等；真实首次effective_at；STATUS版本/身份/原截止一致 |
| T09 | 退款尾部缩期、错误expected_expires_at、重复缩期 | 正确CAS缩期；不一致409；同refund_id稳定回执，不二次缩期 |
| T10 | 多部门成员添加、更新、移除、恢复；跨企业ID | 正确外部映射；旧关联可清理；跨企业拒绝；主管理员保护 |
| T11 | 浏览器平台已登录/未登录、同意/拒绝、真实SaaS回调 | 正常进入正确企业租户；拒绝不建会话；URL不含Secret |
| T12 | 修改state/nonce/verifier/issuer/audience/签名、重复code | 拒绝登录，无越权；重新发起新授权，不反复重放旧code |
| T13 | SaaS会话中用户禁用、授权撤回、到期、安全下架 | UserInfo或本地租户校验失败即拒绝访问，不等会话自然到期 |
| T14 | 对账分页、原始与退款后期限、历史记录、越权请求 | 提供方只查本产品；日期无歧义；不暴露成本或Secret |

每例留存脱敏的请求ID/幂等键、订单号、预期与实际状态、UTC时段及回执摘要。不得留存Bearer、授权码、Secret、完整身份证或客户业务数据。真实第三方完成其自有环境上述矩阵后才可称其联调通过；平台模拟SaaS通过不替代供应商验收。


## 8. 用户单点登录（OIDC）

本节用于用户浏览器登录，不能替代机器接口认证。SaaS 作为 OIDC 客户端，平台作为 OP，采用授权码 + PKCE S256。SaaS 配置 `oidc_sso` 凭据和 `resource_server` 校验信息；平台内部机器客户端不需要由 SaaS 配置，禁止把机器 Token 当作用户身份。

### 8.1 登记与客户端配置

产品登记时填写第1.4节的稳定SaaS应用访问地址和精确OIDC回调地址。四阶段审核通过后平台自动创建独立用户登录客户端，无需提供方手工调用创建接口。提供方企业超级管理员、管理员通过第 1.2 节统一下载文件的 `oidc_sso` 分区获得配置，可重复下载，不轮换密钥。`POST /api/products/{product_id}/oidc-client` 仅作为受控兼容配置接口；其创建时的临时返回不替代统一下载功能。客户端密钥必须由服务端安全保存，浏览器不得保存 Secret 或 OIDC Token。

发现文档及公钥按下载文件的 discovery_url/jwks_url 使用。生产回调使用HTTPS、精确匹配白名单、不允许通配符；开发HTTP须由平台“应用接入地址允许HTTP”显式授权，不仅限localhost。issuer是签发标识，网络代理的内外地址映射需经平台配置，不能为了可达性擅改issuer或接受任意端点。

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

当前授权请求必需client_id、redirect_uri、enterprise_id、state、nonce及code_challenge；state与nonce各16～255字符，S256挑战必须是43字符base64url值。code_verifier为RP保存的高熵随机值，按PKCE规范43～128字符。不要发送当前平台未支持的prompt、login_hint等额外参数；重复查询参数和不支持的Scope会拒绝。`openid`必需，profile与email可选，本规范示例使用三者。

PKCE S256生成示意（Python标准库；每次授权重新生成，不能用固定值）：

```python
import base64
import hashlib
import secrets

state = secrets.token_urlsafe(32)
nonce = secrets.token_urlsafe(32)
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode("ascii")).digest()
).rstrip(b"=").decode("ascii")
# 服务端保存state、nonce、verifier、企业上下文和过期时间，绑定浏览器事务Cookie。
# URL只发送challenge；verifier仅在服务端换码时提交。
```

Token端点支持client_secret_basic或client_secret_post，优先使用前者；Secret只在服务端传送，不能把OIDC授权请求放到浏览器后直接用JS持Secret换码。POST必须是form-urlencoded，不接受JSON（415）；当前请求体最大16KiB、最多20字段、单值最多4096字符，重复字段拒绝。

换码成功示意：

```json
{"token_type":"Bearer","access_token":"opaque-user-access-token","expires_in":300,"scope":"openid profile email","id_token":"signed-rs256-id-token"}
```

access_token是opaque随机字符串，不是可通过JWKS解码的JWT。只有id_token使用RS256。错误换码常见invalid_client、invalid_grant或PKCE错误；平台端可返回OAuth错误对象或HTTP detail，不要把任意200以外响应解析成成功Token。已兑换code再次使用可能撤销其关联访问令牌，应重新走完整授权流程。

### 8.3 验签与租户会话

RP 使用成熟 OIDC 库，以受信任发现文档及 JWKS 验证 RS256 签名、issuer、audience、exp、nonce、at_hash；不得接受其他签名算法或由 Token 自行指定的外部密钥地址。使用 `GET {issuer}/oidc/userinfo` 和 Bearer 用户令牌再次获取身份。

ID Token 与 userinfo 的 `sub`、`enterprise_id`、`subscription_id`、`tenant_id` 必须一致，并匹配本次企业上下文和已通过 OPEN 开通的租户。映射使用 issuer + sub + 企业 + 租户，不得仅按邮箱匹配已有账户。

平台UserInfo成功示例（profile/email未请求时对应字段不保证返回）：

```json
{"sub":"platform-user-id","enterprise_id":"platform-enterprise-id","subscription_id":"platform-subscription-id","tenant_id":"tenant-10001","name":"张三","email":"user@example.com","email_verified":true}
```

UserInfo是普通JSON身份对象，不是ID Token，不保证有iss/aud/exp/nonce/at_hash，也不返回product_id或企业管理员角色。产品关联须通过已信任的OIDC客户端和本地产品配置判断。email可以为空，email_verified是实际邮箱验证状态，不能以其值推断个人实名或管理员权限。

UserInfo无效/过期/撤销Token通常返回401；当前用户、企业成员、授权或订阅不满足资格时还可能返回403。两者均应阻止创建/继续使用业务会话，网络异常也不能默认为已授权。第三方本地管理员身份应使用OPEN/主管理员USER_ASSIGN的持久化映射，不根据邮箱或用户名猜测。

验证通过后 RP 创建独立 HttpOnly 会话；会话不得超过用户令牌期限。受保护请求需重新校验平台 userinfo 和本地租户状态；用户禁用、授权撤回、订阅到期、安全下架及租户关闭均拒绝访问。用户退出只清除 RP 会话，不意味着平台账户全局退出。平台、RP 审计和访问日志不得记录 Secret、Token 或授权码。

### 8.4 验收边界

必须验证真实浏览器登录、同意/拒绝、错误 state/nonce/PKCE、错误 issuer/audience/签名、授权码重放、跨企业拒绝、普通成员授权撤回和到期/禁用拒绝。开发模拟 RP 使用本地持久化会话及实际平台协议；这不代表第三方生产系统已经完成联调。多副本 RP 应使用共享会话存储，不能直接复制本开发用单副本 SQLite 部署。

### 8.5 OIDC端到端时序

```text
用户浏览器           SaaS(RP)服务端              平台(OP)
   |--稳定应用入口------>|
   |                    |--保存state/nonce/verifier与浏览器Cookie
   |<--302到authorize---|
   |----------------GET /oidc/authorize------------>|
   |<---------------登录及用户同意页面---------------|
   |----------------同意/拒绝---------------------->|
   |<--------------302到精确callback?code&state-----|
   |--回调-------------->|
   |                    |--验证state/Cookie/一次性事务
   |                    |--POST /oidc/token + PKCE--->|
   |                    |<--access_token/id_token-----|
   |                    |--GET JWKS，校验签名及claims
   |                    |--GET /oidc/userinfo-------->|
   |                    |<--用户/企业/订阅/租户身份-----|
   |                    |--核对本地已开通租户及用户映射
   |<--HttpOnly会话------|
   |--业务请求---------->|--继续校验UserInfo及本地有效期
```

平台登录/同意页面由平台维护，RP不需要收集平台用户密码，也不代替用户静默同意。普通企业成员只有已获SaaS访问授权且满足当前资格才可登录；买方企业管理员操作权限与普通成员应用使用权限不是一回事。用户中途拒绝或错误返回时，RP清理一次性事务并展示可重试提示，不建立会话。

### 8.6 发现配置及不支持的扩展

当前Discovery公布授权码code、grant_type=authorization_code、subject_type=public、RS256、Scope=email/openid/profile、client_secret_basic/client_secret_post及S256。必须核对发现中的issuer与下载配置相同，并验证所有端点处于预期可信地址；不能根据任意HTTP重定向接受其他授权服务器。

当前未提供refresh_token、动态客户端注册、设备码、隐式模式、OIDC前后通道全局退出或RP发起全局注销端点。不要向本平台假设存在的 `/oidc/logout` 发请求；SaaS退出仅清理自身会话。发现接口可能因平台签名密钥/issuer未就绪返回503，须先修复配置再联调，不绕过验签。真实网络部署须保证浏览器可达授权页及SaaS回调，RP服务端可达Token/JWKS/UserInfo/内省端点；回调由浏览器跟随平台重定向访问，不是平台服务端主动调用。

## 9. 订单期限与第三方权益对账

### 9.1 统一时间和订单上下文

新冻结的管理请求附加 `reconciliation_context`。本节补充前述示例，原管理字段及 OAuth/OIDC 认证方式不变。已经持久化的历史操作仍按原请求重试，不为补字段而更换幂等内容。

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 整数 `1`，本对账上下文版本，不是接入配置下载格式版本 |
| `event_id`、`event_type` | 稳定事件标识及 OPEN/RENEW/CHANGE/STATUS 等管理操作名 |
| `product_id`、`enterprise_id`、`subscription_id`、`tenant_id` | 产品、买方企业、平台订阅和第三方租户关联；创建租户前 tenant_id 可为空 |
| `order_id`、`order_no` | 本次关联的平台订单ID和可读订单号；用户/部门等纯租户操作可为空 |
| `order_starts_at`、`order_ends_at` | 关联订单的服务时段，不是整个租户的累计有效期 |
| `subscription_expires_at` | 本次操作对应的订阅总体截止时间；续费后含已交付续费，退款时为缩期后的时间 |
| `effective_at`、`effective_at_semantics` | 时间及口径；平台交付锚点不等于第三方实际切换回执时间 |
| `period_status` | `provisional` 表示尚未确定实际时段；`confirmed` 表示已冻结的信息，纯租户操作仍允许无单一订单 |
| `affected_orders` | 版本切换涉及的订单时段列表，包含订单关联及剩余时段；不得只按一笔来源订单计算所有未来续费 |

所有时间使用带时区的 UTC ISO 8601。时段为 `[开始时间, 截止时间)`，到达截止时间即不再有效。未确定日期用 `null` 并说明口径，不用当前时间、Token过期时间或任意默认日期代替。

首次 OPEN 只准备租户，实际交付尚未完成，订单时段可以为空且为 provisional。平台随后使用冻结的 RENEW 激活时段并回读 STATUS。普通续费的订单开始时间是新增续费段开始时间，不能重置已购买的旧时段。版本 CHANGE 不改变总体截止日期；以第三方返回的首次 `effective_at` 证明实际版本切换，不能用上下文中的平台时间代替。退款缩期仍执行第3.6节的原截止、新截止及 refund_id 校验。

第三方应持久化关联上下文及执行回执；对同一幂等键重试返回首次结果，不再次延长/缩短服务或覆盖实际生效时间。上下文不是新的授权来源，仍须校验平台Token及产品、企业、订阅、租户归属。

模拟SaaS对含上下文的新变更请求附加以下回执，STATUS返回最近一次实际操作保存的上下文及回执。查询本身不覆盖它；重放旧变更不回退最新状态：

```json
{
  "reconciliation_context": {
    "schema_version": 1,
    "event_id": "delivery:sub-1:order-1:RENEW",
    "event_type": "RENEW",
    "product_id": "product-1",
    "enterprise_id": "enterprise-1",
    "subscription_id": "sub-1",
    "tenant_id": "tenant-1",
    "order_id": "order-1",
    "order_no": "ORD-example",
    "order_starts_at": "2026-10-19T04:00:00+00:00",
    "order_ends_at": "2026-11-19T04:00:00+00:00",
    "subscription_expires_at": "2026-11-19T04:00:00+00:00",
    "effective_at": "2026-10-19T04:00:00+00:00",
    "effective_at_semantics": "platform_delivery",
    "period_status": "confirmed",
    "affected_orders": []
  },
  "accepted_at": "2026-10-19T04:00:00+00:00",
  "reconciliation_receipt": {
    "received_at": "2026-10-19T04:00:00+00:00",
    "expires_at": "2026-11-19T04:00:00+00:00"
  }
}
```

此为字段示意，不是完整业务响应；`received_at/accepted_at` 是首次受理记录时间，不是资源版本切换时间。CHANGE依然必须返回第3.4节的真实 `effective_at`，退款缩期仍必须返回第3.6节的真实新截止及退款回执。历史操作允许没有新增上下文，不得伪造历史回执。

`effective_at_semantics` 还可为 `provider_receipt`（以独立执行回执为准，通常 effective_at 为空）、`entitlement_cutoff`（退款后的权益截断时间，不是退款发生时刻）。CHANGE的 `affected_orders` 每项为 `order_id/order_no/term_id/order_starts_at/order_ends_at/remaining_starts_at/remaining_ends_at`；`term_id` 是订阅订单时段ID，不是财务经济段ID。

### 9.2 提供方只读权益查询

```http
GET /api/provider/products/{product_id}/entitlements?page=1&page_size=50&order_no=ORD-xxx
Authorization: Bearer <提供方企业管理员的平台登录令牌>
```

仅产品提供企业的活跃超级管理员、企业管理员以及授权的平台管理员可查询。不是购买方接口，不接受购买方API Key，也不复用平台内部机器OAuth客户端密钥；本版不新增向提供方分发机器密钥的行为。

支持 `enterprise_id`、`order_id`、`order_no` 精确筛选，以及带时区的 `starts_before`、`ends_after` 时段交集筛选。`page` 从1开始，`page_size` 默认50、最大100。结果包含 `items/total/page/page_size/has_more`，按稳定顺序翻页。未知的未交付时段返回空值，不推定已开通。

订单原始版本、登记时段、当前权益及退款调整信息应按响应字段口径区分。历史下架/到期订单仍可对账；查询不会续费、扣额、调整资金或修改订单。API返回不包含Secret、买方个人登录资料或内部成本。该接口是权益对账，不代表银行到账核验，第三方须结合业务回执而非仅凭截止日期判断资金状态。

查询结果的 `starts_at/ends_at` 与 `recorded_combination_expires_at` 是登记记录；`combination_expires_at`、SaaS的 `subscription_expires_at` 及 `effective_rights` 区分实际权益。`subscription_id/tenant_id` 用于SaaS关联；`refunds/changes/change_revisions` 用于追踪退款及版本变更。`quota_periods` 是网关额度周期，`segments` 是权益区间，不是清算财务分录。证据缺失或不一致时返回409，不静默恢复原始未退款权益。

## 10. 持久化、排障与交付边界

### 10.1 第三方最小持久化模型

| 记录 | 建议唯一关联及必要内容 |
| --- | --- |
| 产品接入配置 | product_id，对应OIDC client_id、加密保存的Secret、受信任issuer及端点、回调白名单 |
| 租户 | product_id+enterprise_id+subscription_id，外部tenant_id、版本、状态、主管理员平台ID、真实expires_at |
| 用户映射 | tenant_id+平台user_id，稳定external_user_id、激活状态、部门集合 |
| 部门映射 | tenant_id+平台映射ID，稳定external_department_id、父部门与状态 |
| 幂等操作 | product_id+幂等键，规范化请求摘要、首次完整响应、受理时间；同键异请求拒绝 |
| 版本回执 | product_id+change_id+direction，首次effective_at、目标版本、绑定幂等键 |
| 退款回执 | product_id+refund_id，原截止、新截止、固定幂等键及首次结果 |
| 订单时段 | order_id/order_no、subscription_id、原始起止及上下文；与租户累计截止分开 |
| OIDC事务/会话 | 短期state/nonce/verifier与浏览器绑定；用户/企业/租户身份、Token及期限；敏感值加密或摘要 |

业务变化与首次幂等结果在同一事务内提交，先写业务后异步写回执会造成崩溃后重复生效。不要截断ID来构建唯一键。原版本切换回执、退款回执、最近管理上下文分别保留，不能互相覆盖。缓存或数据库重启后也须能返回首次响应。

### 10.2 常见问题定位

| 现象 | 优先检查 |
| --- | --- |
| 审核后不能购买 | 产品是否pending_integration；提供方是否已下载配置并完成verify；管理健康与稳定入口是否可达 |
| 交付中/交付异常 | 操作台账具体阶段；OPEN身份、USER_ASSIGN外部ID、STATUS布尔值及根节点、冻结截止是否一致 |
| Token内省失败 | SaaS服务端能否访问真实introspection_url；是否错把OIDC Token当机器Token；客户端状态及产品Scope |
| Discovery正常但不能登录 | 逐产品client_id/Secret、精确回调、PKCE与nonce、用户购买/授权资格；不能只用全局连接检查替代浏览器测试 |
| 续费时间不一致 | 是否把月份本地累加、固定30天、丢失微秒/时区；应采用平台传入截止，检查旧幂等请求重放 |
| OIDC callback失败 | Cookie绑定、state、一次性code、verifier是否来自同一事务；代理地址、issuer与回调是否匹配 |
| UserInfo 401/403 | 令牌族、期限、授权码重放，以及用户/企业/成员/订阅当前资格；禁止失败放行 |
| 退款缩期409 | 实际截止与expected_expires_at、剩余续费段、固定refund_id和幂等键；不得用普通RENEW绕过CAS |
| 本地会话仍能访问过期租户 | 是否每次重新校验UserInfo及本地最新期限，是否错误仅依靠Cookie有效期 |

平台不会在管理请求中提供客户的平台密码。第三方不应把机器Secret、OIDC Secret、Token、code或verifier拼进应用访问URL。关闭租户后的保留周期不是无授权继续访问的宽限期。

### 10.3 已实现与限制

- 已实现：平台统一机器OAuth、产品级OIDC授权码+PKCE、管理接口调用、订阅时段冻结、版本切换回执、退款尾部缩期、成员/部门同步和提供方只读权益查询。
- 普通续费无expected_expires_at CAS字段；退款缩期有。不要把建议扩展写成当前必传协议。
- 上下文回执为附加关联证据，平台核心交付仍按各操作规定的根节点身份、状态及期限校验，并非所有附加字段都有强制自动核验。
- 提供方权益查询使用人员登录Token，不是新机器对账客户端，不提供新的权益变更Webhook；当前无需另建该回调。
- HTTP开发接入、内省入口加固、多副本共享会话、网络白名单及生产证书需要部署方配置；本文件不宣称这些生产环境条件自动具备。
- 本文件使第三方可以独立实现协议及按矩阵测试，但必须使用真实产品配置和测试账号；不提供可公开复用的有效Secret或客户测试Token。正式上线仍需针对第三方实际实现执行验收。

### 10.4 规范参考及变更记录

平台接口细节以本文件及当前下载配置为准，标准背景参考 [OAuth 2.0 RFC 6749](https://www.rfc-editor.org/rfc/rfc6749)、[Token Introspection RFC 7662](https://www.rfc-editor.org/rfc/rfc7662)、[PKCE RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)及 [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)。不要求读者另行阅读平台内部设计文档才能理解请求字段。

v1.6：修正普通RENEW完整请求/响应与平台主导期限；修正OPEN、响应根节点、用户映射及单组合规则；补全机器OAuth和OIDC时序、端点责任、字段约束、错误/重试实际边界、持久化与14项联调矩阵。v1.5增加的对账上下文继续保留；接入配置JSON仍为2.0，无凭据轮换。
