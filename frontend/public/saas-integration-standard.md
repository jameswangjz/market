# SaaS 应用第三方接口对接规范

## OAuth2 凭据

平台产品登记生成 `product_id`，购买时生成 `subscription_id`。这两个 ID 用于平台业务识别，不是 OAuth2 客户端 ID。

由数据集运营服务管理平台在 SaaS 产品审核通过时生成 `client_id/client_secret`、Token 地址等接入信息。产品所有者可登录平台并重复下载 OAuth 凭据文件，然后将信息配置到第三方 SaaS 应用中，完成 OAuth 认证和后续接口认证。

获取 Token：

```http
POST {token_url}
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}&scope={scope}
```

## 业务调用

```http
POST {base_url}/isv.php
Authorization: Bearer {access_token}
Content-Type: application/json
X-Request-Id: {platform_operation_id}
```

所有请求使用 JSON，并带 `type` 字段。成功返回 `ret=0`，失败返回 `ret=1`、`msg`、可选 `code` 和 `retryable`。相同 `X-Request-Id` 必须幂等，不能重复创建租户或扣费。

接口类型：

| type | 主要字段 | 结果 |
| --- | --- | --- |
| `OPEN` | `enterprise_id`、`enterprise_name`、`product_id`、`subscription_id`、`version`、`billing_cycle` | `tenant_id`、`app_id`、`url` |
| `CLOSE` | `tenant_id`、`subscription_id` | `status=closed`，数据至少保留 1 个月 |
| `RENEW` | `tenant_id`、`subscription_id`、`billing_cycle`、`version` | 新到期时间 |
| `CHANGE` | `tenant_id`、`subscription_id`、`from_version`、`to_version`、`change_type` | 新版本和状态 |
| `USER_ASSIGN` | `tenant_id`、`user_id`、`username`、`name`、`email`、`phone`、`department_id` | 第三方用户 ID |
| `USER_UNASSIGN` | `tenant_id`、第三方 `user_id` | `status=deleted` |
| `DEPT_CREATE` | `tenant_id`、`department_id`、`parent_id`、`name` | 第三方部门 ID |
| `DEPT_REMOVE` | `tenant_id`、第三方 `department_id` | `status=deleted/disabled` |

升级订单支付差价后调用 `CHANGE`；降级按剩余周期比例退款后调用 `CHANGE`；续费支付后周期顺延；关闭后 30 天内通过 `OPEN` 恢复原租户。完整请求/响应示例、错误处理、重试和平台接口清单见项目文档 `docs/saas-integration-standard.md`。
