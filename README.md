# Market 数据集运营服务管理平台

首版实现数据/服务产品运营的内部闭环：用户与企业入驻、产品审核、订单四域状态机、模拟支付、交付验收、售后、清算分账和审计。

## 本地运行

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=sqlite:///./market.db uvicorn app.main:app --reload --port 8000

cd frontend
npm install
npm run dev
```

开发环境演示账号：`admin@market.local` / `Admin123!`

## Kubernetes

[K8S2 无人值守发布部署手册](<docs/06 Market K8S2 无人值守发布部署-20261120113740.md>)

[完整部署手册](<docs/05 Market 独立部署手册-20261119175225.md>)覆盖独立安装、离线发布、数据库选型、验收与回退。

安装前通过 `--db-type` 选择数据库。安装节点需有 Python 3 和 PyYAML。

| 方式 | 参数 | 部署结果 |
| --- | --- | --- |
| 内置 PostgreSQL（默认） | 省略全部数据库参数，或只传 `--db-type Postgres` | 使用 `market` 命名空间的 `market-postgres:5432/market`，首次安装创建内置库 |
| 指定 PostgreSQL | `--db-type Postgres` 加地址、用户名和密码文件 | 使用指定库。端口默认 5432，数据库名默认 `market` |
| 指定达梦 | `--db-type Dameng` 加地址、用户名和密码文件 | 使用指定达梦实例与登录用户的 Schema，端口默认 5236 |

```bash
# 默认内置 PostgreSQL
bash scripts/deploy.sh --db-type Postgres

# 指定 PostgreSQL
bash scripts/deploy.sh --db-type Postgres --db-address pg.example:5432 \
  --db-user market_app --db-password-file /secure/market-db-password --db-name market

# 指定达梦
bash scripts/deploy.sh --db-type Dameng --db-address dm.example:5236 \
  --db-user MARKET_APP --db-password-file /secure/market-db-password

kubectl -n market get pods
kubectl -n market get svc market-web
```

密码文件填写一行原始密码，权限建议为 `0600`。指定外部库时，地址、用户名和密码文件必须同时提供。安装器将连接配置写入 `market-db-connection` Secret。外部库需提前创建 PostgreSQL 数据库或达梦应用用户，并授予建表和应用运行权限。

离线 Flux 安装使用 [站点配置生成器](scripts/render_market_env2.py)，在现有站点参数后追加上述相同的数据库选项，再应用生成的 `site-secrets.yaml`、`foundation.yaml` 和 `market-flux.yaml`。选择达梦时使用包含达梦驱动的新 OCI 发布包。

这些选项面向新安装。已有部署切换数据库前需协调所有数据库客户端停机和数据迁移。选择外部库时，新建清单省略内置 PostgreSQL 资源，已有 PostgreSQL 和 PVC 保留。达梦模式的 Redis 维护前数据库备份仍需按达梦流程处理。

部署脚本为本地镜像统一生成不可变标签：`YYYYMMDDHHMMSS-GitSha`。如果部署目录没有 Git 元数据，需显式传入 `GIT_SHA`，例如：`GIT_SHA=d39193140003 bash scripts/deploy.sh`。

首版使用 NodePort `30080` 暴露前端，暂不依赖 Ingress、域名和 HTTPS 证书。对外连接器和国家数据基础设施接口不在首版范围内。
