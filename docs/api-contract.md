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
- `POST /products`
- `GET /products/{id}`
- `POST /products/{id}/submit`
- `POST /products/{id}/publish`

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
