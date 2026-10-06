# SaaS 类型应用需求分析与开发计划

版本：v1.0  
适用范围：产品类型为“SAAS 类型应用”、交付方式为“租户/权限开通”的产品

## 1. 已确认需求

- 平台主动调用第三方 SaaS 服务，不依赖第三方回调。
- 正式认证采用 OAuth2 `client_credentials`，使用 `client_id/client_secret` 获取 Access Token。
- 兼容参考目录中的 HMAC-SHA256 示例，以适配旧版或演示服务。
- 支持月付、季付、年付和永久授权。
- 升级支付当前版本和目标版本的差价。
- 降级按照剩余周期比例计算退款，并自动原路退款。
- 允许提前续费，续费周期从当前订阅到期时间之后顺延。
- 删除用户可通过增加用户接口恢复。
- 租户关闭后至少保留一个月，一个月内允许恢复。
- 平台支持部门和部门人员同步到第三方 SaaS。
- 一个企业可以购买同一 SaaS 产品的多个版本和多个独立租户。

## 2. 参考接口分析

参考 `isv-demo-php-master` 提供以下操作类型：

```text
OPEN、CLOSE、DELETE、RENEW、CHANGE、
DEPT_CREATE、DEPT_REMOVE、USER_ASSIGN、USER_UNASSIGN
```

示例代码中 `isv.php` 使用 `type` 字段分派接口，成功响应包含 `ret=0`，失败响应包含 `ret=1` 和 `msg`。`OPEN` 返回 `app_id` 和 SSO 地址。示例管理接口实际使用 HMAC-SHA256；`sso.php/callback.php` 使用 OAuth2 授权码完成用户 SSO。本平台正式实现以已确认的 OAuth2 Client Credentials 为主，并保留 HMAC 兼容适配器。

## 3. 功能范围

### 3.1 产品和版本

- SaaS 产品支持多个可售版本。
- 每个版本独立维护月、季、年、永久价格。
- 每个版本维护用户数、部门数、存储空间和能力描述等限制。
- 版本支持草稿、销售中、停售状态。
- 产品登记弹框选择 SaaS 类型时显示《SaaS 应用第三方接口对接规范》下载链接。

### 3.2 租户订阅

- 首次购买生成独立 SaaS 订阅和第三方租户。
- 同一企业同一产品允许多个独立租户。
- 每个订阅独立保存版本、到期时间、第三方租户 ID 和同步状态。
- 租户关闭后进入保留期，默认保留 30 天。
- 保留期内可恢复，超过保留期不保证可以恢复。

### 3.3 用户和部门

- 支持 SaaS 增加、删除和恢复用户。
- 支持创建、删除部门。
- 支持平台部门同步至 SaaS。
- 支持平台部门成员同步至 SaaS。
- 保存平台 ID 与第三方 ID 的映射关系。

### 3.4 订单和版本变更

- 订单保存版本快照和计费周期。
- 升级生成差价支付订单。
- 降级生成退款订单，按剩余周期比例计算并自动原路退款。
- 续费正常支付，新的周期从原到期时间之后顺延。
- 订单保存业务类型：开通、续费、升级、降级、关闭、恢复、退款。

## 4. 数据模型

- `saas_product_versions`：SaaS 版本和多周期价格。
- `saas_integration_configs`：第三方地址、OAuth2 Token 地址、Client ID、密钥、Scope、兼容认证模式。
- `saas_subscriptions`：企业、产品、版本和第三方租户订阅。
- `saas_user_mappings`：平台用户和第三方用户映射。
- `saas_departments`：平台部门和第三方部门映射。
- `saas_operations`：所有第三方操作流水、请求、响应、重试和结果。
- `orders` 扩展版本 ID、版本快照、计费周期、订阅 ID、业务类型和关联订单字段。

## 5. 后端接口边界

```text
GET/POST       /api/products/{product_id}/saas-versions
GET/PUT       /api/products/{product_id}/saas-integration
POST          /api/products/{product_id}/saas-subscriptions
GET           /api/saas-subscriptions
POST          /api/saas-subscriptions/{id}/renew
POST          /api/saas-subscriptions/{id}/change-version
POST          /api/saas-subscriptions/{id}/close
POST          /api/saas-subscriptions/{id}/restore
POST          /api/saas-subscriptions/{id}/users
DELETE        /api/saas-subscriptions/{id}/users/{user_id}
GET/POST/DELETE /api/saas-subscriptions/{id}/departments
GET           /api/saas-subscriptions/{id}/operations
```

第三方适配器统一提供：

```text
create_tenant, close_tenant, delete_tenant, renew,
change_version, assign_user, unassign_user,
create_department, remove_department
```

## 6. 分阶段开发计划

### 阶段一：接口适配

- OAuth2 Client Credentials Token 客户端。
- Token 缓存、过期刷新和一次重试。
- HMAC 兼容适配器。
- 第三方请求、响应和错误码标准化。
- 幂等键、超时、重试和操作流水。

### 阶段二：版本和订阅

- 版本管理和四类计费周期。
- 多租户订阅模型。
- 产品登记 SaaS 字段校验。
- 首次开通和关闭恢复。

### 阶段三：组织同步

- 用户增加、删除、恢复。
- 部门创建、删除和人员同步。
- 第三方 ID 映射和失败重试。

### 阶段四：订单和计费

- 首次开通订单。
- 提前续费顺延。
- 升级差价订单。
- 降级比例退款订单。
- 自动原路退款。
- 关闭、恢复和退款审计。

### 阶段五：前端和文档

- SaaS 对接文档下载入口。
- 版本价格管理。
- 企业订阅列表。
- 订阅操作和同步状态。
- 订单版本和业务类型展示。

### 阶段六：部署和测试

- K8S 配置第三方凭证和服务地址。
- API、数据库、Redis 和操作流水联调。
- 接口、订单、退款、权限、异常和回归测试。

## 7. 验收标准

- OAuth2 Token 可以获取、缓存、过期刷新。
- SaaS 产品可以维护多个版本和不同周期价格。
- 同一企业可以建立多个独立租户订阅。
- 开通、续费、升级、降级、关闭和恢复状态正确。
- 升级仅支付版本差价。
- 降级按剩余周期比例退款并生成退款流水。
- 提前续费后到期时间正确顺延。
- 用户、部门和部门人员同步状态可追踪。
- 第三方失败可以重试且不会重复创建租户或用户。
- 订单可查看产品版本、订阅 ID 和业务类型。
- SaaS 对接标准文档可从产品登记弹框下载。
