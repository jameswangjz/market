# SaaS 应用第三方接口对接规范

## 1. 对接模式

平台采用“统一运营平台调用第三方 SaaS”的模式，不要求第三方向平台回调。每个 SaaS 产品独立配置 `base_url`、操作路径、OAuth2 Token 地址、`client_id`、`client_secret` 和超时。认证采用 OAuth2 `client_credentials`：

```http
POST {token_url}
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}&scope={scope}
```

第三方返回 `access_token` 和可选的 `expires_in`。平台缓存令牌并在有效期提前刷新，请勿记录密钥或令牌。

## 2. 业务调用约定

```http
POST {base_url}/isv.php
Authorization: Bearer {access_token}
Content-Type: application/json

{"type":"OPEN", ...}
```

成功返回 `ret=0`，失败返回 `ret=1` 和 `msg`。所有操作使用平台生成的幂等键记录台账，失败可重试，成功幂等键不得重复创建租户或重复扣费。

## 3. 操作与字段

| 操作 | 必填字段 | 返回建议 |
| --- | --- | --- |
| `OPEN` | `enterprise_id`、`enterprise_name`、`product_id`、`version`、`billing_cycle` | `tenant_id`、`app_id`、`status`、`url` |
| `CLOSE` | `tenant_id` | `status` |
| `RENEW` | `tenant_id`、`billing_cycle` | `status`、`expires_at` |
| `CHANGE` | `tenant_id`、`from_version`、`to_version`、`change_type` | `status`、`version` |
| `USER_ASSIGN` | `tenant_id`、`user_id`、`username`、`name`、`department_id` | `user_id` 或 `external_user_id` |
| `USER_UNASSIGN` | `tenant_id`、`user_id` | `status` |
| `DEPT_CREATE` | `tenant_id`、`department_id`、`parent_id`、`name` | `department_id` 或 `external_department_id` |
| `DEPT_REMOVE` | `tenant_id`、`department_id` | `status` |

第三方应保证关闭租户后至少保留 1 个月；删除用户可通过再次执行 `USER_ASSIGN` 恢复；`CHANGE` 支持升级和降级。一个企业可以购买同一产品的多个版本或多个独立租户。

## 4. 计费与生命周期

支持 `monthly`、`quarterly`、`annual`、`perpetual`。续费正常支付，新周期顺延到当前到期时间之后。升级订单支付版本差价，支付确认后调用 `CHANGE`；降级按剩余周期比例计算退款并调用 `CHANGE`，平台生成原路退款记录。关闭租户后 30 天内可恢复，恢复调用 `OPEN` 并沿用原租户标识。

## 5. 平台接口

- `GET/POST /api/products/{product_id}/saas-versions`
- `GET/PUT /api/products/{product_id}/saas-integration`
- `POST /api/products/{product_id}/saas-subscriptions`
- `POST /api/saas-subscriptions/{id}/renew`
- `POST /api/saas-subscriptions/{id}/change-version`
- `POST /api/saas-subscriptions/{id}/close`、`/restore`
- `GET/POST/DELETE /api/saas-subscriptions/{id}/users`
- `GET/POST/DELETE /api/saas-subscriptions/{id}/departments`
- `GET /api/saas-subscriptions/{id}/operations`
