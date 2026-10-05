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

```bash
kubectl apply -f k8s/all-in-one.yaml
kubectl -n market get pods
kubectl -n market get svc market-web
```

首版使用 NodePort `30080` 暴露前端，暂不依赖 Ingress、域名和 HTTPS 证书。对外连接器和国家数据基础设施接口不在首版范围内。
