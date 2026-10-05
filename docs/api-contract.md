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
- `GET /products/{id}`
- `POST /products/{id}/submit`
- `POST /products/{id}/publish`

Product registration requires or supports the following metadata: catalog name, product/service name, product type, provider name/type, description, version, usage scenarios, delivery method, price, pricing strategy, security level, authorization conditions, data source statement, and compliance statement.

## Orders

- `GET /orders?status=&q=`
- `POST /orders`
- `GET /orders/{id}`
- `POST /orders/{id}/transition` body `{action,reason}`

## Operations

- `GET /delivery-tasks`
- `GET /after-sales`
- `GET /settlements`
- `GET /audit-logs`
- `GET /users`

The connector and national data infrastructure external APIs are intentionally excluded from this version.
