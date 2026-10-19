# API应用提供方接入统一网关接口与规范

版本：v2.0；管理协议：market-api-management-v1；管理请求schema_version：整数1。

本文件面向API提供方及平台集成人员，不包含购买方凭据申领/轮换接口。模型推理API作为API应用接入；“模型”商品仍按文件交付，历史model_api交付兼容本管理协议。示例域名、标识及Token都是占位值，实际接入须取得真实产品配置和测试环境。

## 1. 架构与实施顺序

```text
业务数据面：购买方应用 -> 平台APISIX网关 -> 提供方版本业务上游URL
管理控制面：平台交易/交付进程 -> API服务管理接口地址 -> 提供方权益台账
认证签发：平台内部客户端 -> 平台OAuth授权服务器 -> 产品级机器Bearer
认证校验：提供方服务端 -> 平台Token内省端点 -> active/产品/Scope
```

网关是凭据、期限、频次、额度和计费来源的执行权威。管理接口负责提供方自身开通记录、版本/权益同步及执行回执，不替代网关，不自行发起买方付款、退款或清算。

1. 提供方实现业务API、业务健康检查及独立管理接口，保存幂等结果。
2. 登记API商品及Logo、管理地址；每版本填写上游URL、说明、月价/月成本及网关策略。
3. 完成业务、质量、安全、运营审核。平台创建产品机器OAuth客户端，商品进入待接入验证。
4. 提供方取得OAuth配置，在服务端配置产品标识、内省地址、稳定issuer、audience和Scope。
5. 平台验证版本业务健康及网关路由，成功的版本才可购买；此检查不替代管理接口的端到端测试。
6. 买方支付后，平台冻结期限和管理请求，执行提供方及网关核验后完成交付。
7. 后续续费、升降级、退款权益调整和到期按本规范同步，并通过第9节核对账目关联。

提供方业务不必部署在平台K8S内，但必须从平台受控网络可达。不需要自建OAuth授权服务器，也不要求实现OIDC用户SSO。

## 2. 登记地址与每版本配置

| 地址 | 字段及用途 | 示例 |
| --- | --- | --- |
| API服务管理接口地址 | 产品integration_api_url，完整POST管理端点 | https://provider.example/management |
| 版本上游URL | 版本integration.upstream_url，业务转发地址 | https://provider.example/v1 |
| 版本健康路径 | health_path，与版本上游拼接 | /health |

产品级“提供方后端访问URL”已从新登记界面移除，不再要求重复填写。管理地址独立输入，不从业务URL推导；所有版本可共用管理入口，由product_id及version区分。每版本可独立主机或路径。

每版本还需：版本号、介绍、月价/月成本、每分钟限流、每日配额、订阅月配额、超时时长、健康GET/HEAD方法、上游认证及Scope、路径前缀策略。已售版本配置不可覆盖，应登记新版本。

route_key由平台维护，是网关标识，不是提供方业务路径。入口认证指购买方到网关；上游认证指网关到提供方。strip_prefix决定移除平台 `/gateway/{route_key}` 前缀后怎样拼接版本业务地址，应实测避免路径重复或丢失。

同版本续费延长期限，不叠加同期配额、不清零已用量。日配额按北京时间自然日，月配额按服务生效日划分订阅月；升级/降级保留当前周期用量。平台统一采用最新调用协议，不要求兼容旧调用方式，但商品版本不会未经授权自动切换。

HTTP受系统“应用接入地址允许HTTP”控制，开发可明确允许，生产应使用HTTPS。管理URL不允许用户名/密码、查询参数或片段。私网由平台运维显式配置UPSTREAM_ALLOWED_HOSTS或受控网段，禁止放开回环、链路本地、云元数据及绕过SSRF校验。

## 3. 平台统一OAuth2认证

### 3.1 三类凭证不可互换

| 链路 | 凭证及Scope |
| --- | --- |
| 购买方到APISIX | 平台购买方网关凭据；申领操作不在本文 |
| APISIX到业务上游 | 平台机器Bearer，resource.invoke |
| 平台到提供方管理接口 | 平台机器Bearer，resource.manage |
| 提供方人员下载配置/查询对账 | 提供企业管理员的平台登录Bearer |

resource.invoke不能调用管理接口，resource.manage不能代替业务调用。两种机器Token由同一平台授权服务器签发、绑定产品，不代表某个买方用户或租户。

### 3.2 完整流程

```text
平台进程/APISIX        平台OAuth授权服务器            提供方
     |--内部client_credentials--->|
     |<--access_token/Scope-------|
     |----------------业务/管理请求+Bearer-------------->|
     |                            |<--POST introspect----|
     |                            |---active及claims----->|
     |                            |       校验Scope/产品/业务身份
     |<----------------实际结果及管理回执-----------------|
```

平台内部使用产品client_id/client_secret向自身 `/oauth/token` 发送form-urlencoded，grant_type=client_credentials，scope按链路选择resource.invoke或resource.manage。提供方不实现另一取Token接口，也不用OIDC Secret或买方API Key取机器Token。

当前机器有效期约3600秒，claims含iss/aud/client_id/product_id/scope/iat/exp/jti，aud为market-resource。Token exp是认证有效期，不是客户服务截止日期。

提供方收到Authorization Bearer后，在服务端请求：

```http
POST {introspection_url}
Content-Type: application/x-www-form-urlencoded

token={received_machine_bearer}
```

```json
{"active":true,"iss":"https://platform.example","aud":"market-resource","product_id":"product-1","scope":"resource.manage"}
```

仅布尔active=true合格；检查预配置的issuer、audience、product_id及完整Scope值。未知/过期/撤销Token拒绝，内省不可达失败关闭，不允许失败放行。当前机器JWT是HS256，由平台验签；提供方通过内省，不使用OIDC JWKS或索要平台JWT签名密钥。当前内省未要求额外资源服务器客户端认证，生产须评估TLS、网络入口限制、限流及认证加固，不能假设已启用mTLS。

管理HTTP401会使平台清除对应旧Token缓存，后续业务重试再取Token，不在该次失败请求中立即无限重放。上游业务Token由APISIX缓存。提供方不能只看JWT未过期而忽略客户端撤销或产品归属。

### 3.3 获取实际配置

提供方已实名、已激活的所属企业超级管理员/管理员，从产品页面下载：

```http
GET /api/products/{product_id}/oauth-credentials-download
Authorization: Bearer <提供方人员平台登录Token>
```

当前API下载是受控TXT附件，不是SaaS的OIDC JSON。保留原client_id/client_secret/token_url等兼容字段，新增api_management_url、api_management_protocol、api_management_scope、api_business_scope。提供方验证Token主要使用product_id、introspection_url、issuer、audience及Scope。文件可能包含敏感Secret，须服务端妥善保存，不交购买方、不放浏览器或仓库；不得因拥有文件而代平台任意改动客户权益。

已配置管理地址的产品在审核时获得resource.manage授权；已有客户端ID/Secret/停用状态不自动改变。K8S内部market-api:8000不保证第三方可达，平台须协调可达内省端点和稳定issuer，不能擅改已签发Token中的issuer。本文不提供有效测试Secret。

## 4. 业务API与健康检查

APISIX按版本转发提供方业务HTTP路径、参数和响应，替换上游Authorization为resource.invoke机器Bearer。提供方需处理其声明的方法/Content-Type，文件返回正确Content-Disposition，流式响应按约定返回chunk或SSE，并在网关超时内响应。业务写入需自行实现业务幂等，管理幂等键不自动保护业务请求。

当前不能假定所有原生APISIX请求都携带经过平台签名的企业/租户/订单头；机器Token也没有买方身份。不能信任客户端随意传入的X-Market-Enterprise-Id等头来授权。平台入口负责客户授权，提供方台账通过管理消息和第9节查询关联；业务另需客户上下文时须单独约定可信注入，不能依据本文未承诺的请求头实现租户鉴权。

业务健康按版本upstream_url+health_path访问，最多10秒、不跟随重定向、需2xx。可提供只读公开健康接口，但不能因此取消业务鉴权。管理接口与业务健康不是同一URL，登记的管理地址必须是真实管理程序。

上游业务错误不等于没有扣额：平台退款用量采用网关实际扣额事实。调用失败统计、银行到账和清算回款不由提供方自行推断。

## 5. 管理HTTP与完整数据契约

### 5.1 HTTP、响应和幂等

```http
POST {产品登记的API服务管理接口完整地址}
Authorization: Bearer <resource.manage机器Token>
Content-Type: application/json
Idempotency-Key: <固定操作键>
```

当前管理调用超时10秒、不跟随跳转；变更后再STATUS回读。请求为JSON对象，成功HTTP 2xx且整数ret=0，关键结果必须在根节点，不仅放入data。

绑定product_id+enterprise_id+combination_id；generation区分到期再买后的权益代次。tenant_id可为提供方外部客户/租户ID，也允许不创建实体SaaS租户。平台企业产品共享网关凭据，订单独立留存，不按续费订单另开租户。

产品/企业范围内持久化event_id和idempotency_key；先查首次结果，再判断状态。同键同完整请求返回首次结果，同键改内容409。业务变化与首次回执同一事务提交，重启后也不能重复延期。STATUS只读真实当前状态，不回显请求伪装成功。

### 5.2 根字段与周期字段

| 根字段 | 类型及含义 |
| --- | --- |
| schema_version | 整数1 |
| type | OPEN/RENEW/CHANGE/CLOSE/RESTORE/ADJUST/STATUS |
| event_id / idempotency_key | 固定事件标识/操作键，操作键最长128字符 |
| product_id / enterprise_id / combination_id | 产品、购买企业、平台订阅组合 |
| generation | 正整数代次，不能以旧代覆盖新代 |
| tenant_id | 可空外部标识，已有绑定必须一致 |
| order_id / order_no | 本次关联订单ID及可读订单号 |
| version | 对象{id,code}，目标商品版本 |
| subscription_months | 关联订单本次购买月数，不是组合累计月数 |
| starts_at / expires_at | 本次目标组合总体边界，不是Token期限 |
| periods | 完整目标权益时段列表，不是增量 |
| change_id / direction | 变更时附带，direction=upgrade/downgrade/restore |
| refund_id | 退款权益调整时附带，非付款ID |

每个period固定包含order_id、order_no、term_id、starts_at、expires_at、quotas。quotas固定包含rate_limit_per_minute、daily_quota、monthly_quota、quota_amount四个非负整数，布尔值不合格。monthly_quota是该订阅月实际额度；quota_amount为冻结版本元数据，不与monthly_quota相加另造额度。0表示不限；无权益用空periods/closed表达，不能用0表示退款清零。

根starts_at是组合起点。**本笔续费开始/截止，在periods中按本order_id取最小starts_at和最大expires_at**，不能把组合原起点误认为本次续费开始。多月订单可有多个订阅月period，term_id相同、周期不同。

所有时间为UTC ISO 8601（Z或+00:00）、最多6位小数，区间左闭右开。平台计算并冻结，提供方按目标执行，不按请求次数本地加月份。月周期使用原始服务日历锚点；目标月没有该日取月末，后续仍恢复原日，不按自然月或固定30天。

### 5.3 开通完整请求

```json
{
  "schema_version": 1,
  "type": "OPEN",
  "event_id": "event-open-1",
  "idempotency_key": "delivery:order-1",
  "product_id": "product-1",
  "enterprise_id": "buyer-enterprise-1",
  "combination_id": "combination-1",
  "generation": 1,
  "tenant_id": null,
  "order_id": "order-1",
  "order_no": "ORD-example-1",
  "version": {"id": "version-1", "code": "v1"},
  "subscription_months": 1,
  "starts_at": "2026-10-19T08:00:00+00:00",
  "expires_at": "2026-11-19T08:00:00+00:00",
  "periods": [{
    "order_id": "order-1", "order_no": "ORD-example-1", "term_id": "term-1",
    "starts_at": "2026-10-19T08:00:00+00:00", "expires_at": "2026-11-19T08:00:00+00:00",
    "quotas": {"rate_limit_per_minute": 60, "daily_quota": 1000, "monthly_quota": 10000, "quota_amount": 10000}
  }]
}
```

### 5.4 完整回执要求

响应保留请求中全部协议、操作、身份、版本、订单、日期、periods及可选变更/退款字段，并增加ret/status/effective_at；tenant_id可改为实际生成的稳定外部ID。以下Python示意产生完整响应，不是要求使用Python：

```python
# 在真实业务事务内完成资源变更后，保存该首次完整响应。
receipt = dict(validated_request)
receipt.update(ret=0, tenant_id=actual_tenant_id,
               status="closed" if receipt["type"] == "CLOSE" else "active",
               effective_at=actual_first_commit_time_utc)
```

effective_at是第三方首次实际完成本操作的时刻，不使用请求starts_at、付款时间或当前重试时间。开通时API已具备平台冻结时段，区别于SaaS先准备再激活的阶段划分，但OAuth技术体系相同。不得仅返回ret=0而省略证明字段。

## 6. 生命周期接口

### 6.1 OPEN 开通

首次组合按完整目标periods建立权益及台账，返回active，再由STATUS核验。租户标识必须稳定，同键超时重试不得创建多个。身份、时间或周期错误拒绝，平台不因HTTP 200就认定已交付。

### 6.2 RENEW 续费

在续费订单支付成功后调用，type=RENEW，根order_id/order_no为新续费订单，subscription_months是本次月数。periods包含保留旧时段与新增时段，根expires_at为续费后总体截止。提供方应用完整目标，不在当前截止上再加月数。

例：旧订单10月19日至11月19日，续费1个月；根starts_at仍为10月19日，expires_at改为12月19日，新订单period为11月19日至12月19日。提前付款不提前重置未来周期额度。

普通API续费不使用SaaS退款缩期的expected_expires_at。已到期重新购买由平台冻结新的generation和起止日期，受控RESTORE恢复；不补算过期时段，不恢复旧代额度。

### 6.3 CHANGE 升降级和补偿

附change_id与direction。upgrade/downgrade应用目标version及全部periods；restore是补偿恢复原目标，使用独立固定键。平台准备时可能同时冻结正向及补偿计划，不表示两者立即执行。第三方只按实际收到的操作执行。

多笔未来续费也在periods内，不能只改来源订单。保存真实effective_at，保留已用量，不自行重算差价或擅自延长期限。平台核验后继续网关与财务流程，一次CHANGE成功不代表资金或清算成功。

### 6.4 ADJUST 退款权益调整

附refund_id，periods为退款后的完整目标。有限额度API按已批准比例缩减额度，不限额度按比例缩短未用尾部；金额、成本、用量及经济段由平台核定，第三方不另算买方退款。

保留其他订单时段，不因一笔退款关闭全企业组合。没有剩余权益时可用CLOSE；不能以quota=0表示不允许调用。STATUS须返回实际调整后截止及periods，不能用原始未退款日期冒充结果。

### 6.5 CLOSE 关闭

periods为空，返回closed。仅指定产品/企业/组合/代次，不删除全局产品路由，不影响其他买方；保留历史订单和回执。关闭不表示退款/追款资金完成。

到期网关按自身期限拒绝，提供方关闭可稍后同步，不能因其尚未回执而继续放行。手工产品下架不自动关闭已售权益。达到重试上限转人工，不假称提供方已关闭。

### 6.6 RESTORE 恢复

恢复指定绑定及目标periods，例如到期后的新付费购买。generation和日期由平台决定，允许合法同代恢复或新代，不允许旧代覆盖新权益；原关闭及退款回执保留。

### 6.7 STATUS 实际状态

平台将原完整请求type改为STATUS并增加target_type，其他字段供身份和预期关联；查询不能改变资源。响应是最近实际保存的完整变更回执，根type/event_id/idempotency_key仍为最近执行操作，不是直接回显STATUS。

平台比较变更响应与实际STATUS的全部身份、版本、periods、日期、status和effective_at。旧操作重放可返回旧首次回执，但STATUS必须保持最新实际状态；二者不一致时平台拒绝当作当前成功，不能为通过校验回退资源。

## 7. 分阶段执行、并发与错误

首购/续费先提交不可变管理计划、预分配组合/时段ID和日期，此时没有实际交付成功记录；下一阶段执行提供方核验，再建立真实订阅并核对APISIX路由、Consumer和策略。远程成功但响应丢失或平台回滚，仍重用已提交的相同请求和固定日期。未完成计划不能被另一订单越过。

版本及退款计划与原资源准备一起冻结，失败不推进资金完成；双方状态可能需补偿/重试，第三方不能把一次资源调用当整个交易已完成。空历史管理地址保留明确unmanaged，不临时改用现有业务地址或虚构回执。

自动交付每轮最多3次失败尝试、失败间隔10秒，正常阶段推进不算失败；到期关闭独立分阶段执行、最多3次，崩溃租约用于安全恢复。版本/退款由各自Saga调度，不承诺完全相同的时序。

错误建议：无效Token401，Scope/产品不符403，参数400/422，无绑定404，同键异体/状态冲突409，限流429，依赖故障503。平台未按retryable实现通用分类调度，不能只靠该字段指定重试。当前非2xx或错误证明均视为失败，不要返回虚假的active以规避重试。

## 8. 最小持久化与安全

保存产品配置、企业组合/外部租户、generation、订单及periods、每操作原始请求摘要、首次完整响应、真实effective_at、变更/退款关联。唯一键使用完整产品/企业/组合标识，不能截断ID或仅按企业名称去重。

先查幂等再校验状态；CLOSE成功后同键重放仍返回首次结果，不能因已closed而报错误。首次回执与状态同事务提交，旧回执、最近状态、版本/退款记录分别保留，防止互相覆盖。SQLite只是开发模拟方案，正式多副本应使用共享可靠存储。

无效内省、网络失败、缺失身份均失败关闭。凭据、Bearer不得进业务URL/访问日志/错误页。客户端身份头不作为平台证明；只访问固定受信内省端点，不从Token自行指定任意授权服务器。

## 9. 提供方只读权益对账

```http
GET /api/provider/products/{product_id}/entitlements?page=1&page_size=50&order_no=ORD-example-1
Authorization: Bearer <提供企业管理员的平台登录Token>
```

仅产品所属提供企业超级管理员/管理员及授权平台管理员可查，不接受买方API Key或业务/管理机器Token。支持enterprise_id/order_id/order_no精确筛选，带时区starts_before/ends_after按登记时段交集筛选；page从1开始，page_size默认50、最大100。

返回items/total/page/page_size/has_more；包括订单、买方企业、原版本、登记时段、组合/代次、退款及变更关联。starts_at/ends_at和recorded_combination_expires_at是登记历史，effective_rights及combination_expires_at是实际权益。已管理API关联tenant_id及provider_management中的最近已核验状态/操作/截止；无管理绑定明确unmanaged。

旧代记录不自动获新权限；periods/segments不是清算分录。接口不会扣额、付款或续费，不返回内部成本/Secret。证据缺失/不一致返回409，不推定缺失用量为0。财务对账仍需支付、退款和清算报告，期限回执不代表银行到账或追款完成。

## 10. 独立联调测试

提供方准备可达管理URL、版本业务URL及健康路径；平台准备独立测试产品、企业、订单和OAuth元数据。管理Token来自受控平台测试，不复用买方API Key，不在文档/代码保存真实Token。

```bash
curl --fail-with-body -X POST "$INTROSPECTION_URL" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "token=$RECEIVED_MANAGEMENT_TOKEN"

curl --fail-with-body -X POST "$API_MANAGEMENT_URL" \
  -H "Authorization: Bearer $PLATFORM_MANAGEMENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: delivery:order-1' --data-binary @payload.json
```

payload.json采用第5节完整请求，其他操作按第6节替换完整目标，不只发送变化字段。开发模拟管理端点为K8S内 `http://market-mock-api:8300/management`，SQLite须挂持久卷；真实第三方不能直接使用内部DNS作为公网地址。公开/health只证明进程就绪，不证明管理及网关链路全通过。

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| A01 | 登记/编辑 | 无重复产品URL，管理地址/各版本上游必填，已售保护不放宽 |
| A02 | Token、Scope、产品错误或撤销 | invoke不能管理、manage不代业务；内省失败不放行 |
| A03 | 首购支付两阶段 | 准备无成功时段/审计，提供方与网关核验后交付 |
| A04 | 提前续费、多月、月末/闰年 | 新订单周期正确，同组合，不重置/叠加同期额度 |
| A05 | 同键重放/异体、超时、重启 | 首次回执持久，异体409，平台回滚后请求不变 |
| A06 | 升级/降级/补偿 | 当前及未来periods正确，独立键，版本/资金分别证明 |
| A07 | 部分/全额退款及其他订单 | 仅缩减批准权益，新截止正确，不用0表达无权 |
| A08 | 到期关闭、新代恢复 | 只影响一个组合，旧代不覆盖，失败如实记录 |
| A09 | 跨企业/产品/组合/租户 | 拒绝越权，不信任客户端伪造身份头 |
| A10 | 较新变更后重放旧操作 | 旧首次回执可重放，STATUS不回退最新状态 |
| A11 | 业务文件/流式/超时/5xx | 透传及网关限额有效，错误统计可追踪 |
| A12 | 对账分页/历史/退款日期 | 订单号一致，原始/实际权益分开，无Secret/成本泄漏 |

留存脱敏事件ID、订单号、请求摘要、首次回执、目标/实际日期及periods、平台任务结果。必须在真实提供方环境执行矩阵才能称该供应商对接通过，模拟测试不替代正式验收。当前支付/退款为平台模拟或人工确认，不代表真实银行已接入。

## 11. 平台控制面与边界

提供方不需要实现平台产品登记或网关配置API。平台人员控制面使用登录Token：GET/PUT `/api/products/{product_id}/versions/{version_id}/integration` 保存/查看版本配置，POST同路径加 `/verify` 验证发布；已售版本不可覆盖。管理接口无单独发布页面，地址在产品登记中维护。

旧订单冻结快照没有integration_api_url时不自动向新管理地址补发历史开通/续费；历史迁移须独立盘点，不改已售快照制造证明，不重置用量。当前不新增OIDC、买方凭据后台、机器对账客户端、权益Webhook或银行自动对账。

标准背景：[OAuth 2.0 RFC 6749](https://www.rfc-editor.org/rfc/rfc6749)、[Token内省 RFC 7662](https://www.rfc-editor.org/rfc/rfc7662)。平台协议以本文件和实际产品配置为准，不依赖另一本内部设计文档猜测字段。

v2.0：移除重复产品地址，新增独立管理URL及resource.manage；覆盖生命周期、订单/组合期限、代次、配额、持久化回执、失败恢复和12项测试矩阵。购买方使用说明仍单独保留，不混入提供方接口。
