# API Contract

Base path: `/api`

## Identity and enterprise

- `POST /auth/send-code`：开发环境验证码固定为 `123456`，不发送真实短信或邮件
- `POST /auth/register`：支持 `email` 或 `phone` 注册；手机号注册需验证码
- `POST /auth/login`：`identifier` 支持邮箱或手机号
- `POST /verification/personal`：提交身份证正反面文件、身份证信息、手机号和企业角色，进入人工审核
- `GET /verification/personal/me`
- `POST /admin/verifications/personal/{id}/review`
- `POST /verification/enterprise`：提交营业执照文件、企业全称、统一社会信用代码、主体类型和法定代表人
- `GET /admin/verifications/enterprise`、`POST /admin/verifications/enterprise/{id}/review`
- `POST /enterprise/invitations`、`POST /enterprise/invitations/{token}/accept`
- `GET /enterprise/members`、`PATCH /enterprise/members/{id}`
- `POST /products/{id}/access-grants`：SaaS 应用由企业超级管理员或企业管理员授予普通成员访问权限

## Platform settings

- `GET/PATCH /admin/settings/notifications`：配置短信平台、邮件服务器、端口、SSL 和发件账号；密码不回显
- `GET/POST /admin/platform-roles`：平台超级管理员分配平台运营、产品、审核、质量、安全、交付和财务角色，并生成临时密码

## Authentication

- `POST /auth/login` body `{email,password}`
- `GET /auth/me`
- `POST /auth/register`

## Dashboard

- `GET /dashboard`

## Products

- `GET /products?status=&type=&q=`
- `GET /product-directories`
- `POST /products`
- `PUT /products/{id}`
- `GET /products/{id}`
- `POST /products/{id}/submit`
- `POST /products/{id}/review` (`approve` / `reject`，驳回必须填写意见)
- `POST /products/{id}/publish`
- `GET /products/{id}/files`
- `POST /files/upload`（可通过 `product_id`、`file_role`、`version`、`description` 绑定产品文件元数据）

Product registration requires or supports the following metadata: catalog name, product/service name, product type, provider name/type, description, version, usage scenarios, delivery method, price, pricing strategy, security level, authorization conditions, data source statement, and compliance statement.

## Orders

- `GET /orders?status=&q=`
- `POST /orders`
- `GET /orders/{id}`
- `POST /orders/{id}/transition` body `{action,reason,refund_amount}`；`approve_refund` 支持部分退款，并记录独立退款单

## Operations

- `GET /delivery-tasks`
- `GET /after-sales`
- `GET /settlements`
- `POST /settlements/generate/{order_id}` 按实际退款金额计算退款追回、可分账净额和各方分账金额
- `GET /audit-logs`
- `GET /users`

The connector and national data infrastructure external APIs are intentionally excluded from this version.
