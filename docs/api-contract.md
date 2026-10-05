# API Contract

Base path: `/api`

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
