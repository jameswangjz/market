# OperationPlatform — project notes for humans and AI agents

Keep this file current. It is the first thing read when someone (or an agent)
picks up this repo. Update it in the same PR as any significant change.

## Purpose
Enterprise data/service marketplace with product review, paid fulfillment,
API/SaaS integration, settlement, auditing and platform support.

## Current status
2026-10-12: SaaS provider OAuth2/OIDC combined download and browser SSO are
deployed. API/model API automatic fulfillment is deployed as
20261012153143-50244a8 in the existing online delivery worker, including initial
buyer credential issuance, encrypted storage and buyer-only JSON download.
API/model runtime and desktop/mobile browser checkout, simulated payment,
worker fulfillment and credential download passed. Runtime acceptance uses exact disposable fixtures;
review approval is a fixture precondition and payments are simulated. Do not
close TRD-BE-012 or full-flow QA solely on these narrower adapter checks.

Durable version-change Saga and monthly signed-ledger integration are deployed
as 20261012165357-a27e25a. Real API/model and mock SaaS upgrade/downgrade worker
checks passed; monthly HTTP checks previously used synthetic ledger fixtures.
Actual worker restart and owned target-route fault compensation passed, as did
actual API/model worker ledger snapshots through isolated monthly HTTP settlement.
All corresponding live fixtures and private schemas were precisely removed;
the protected order/product were unchanged and worker batch was restored to 10.
Web quote submission usability fix deployed as 20261012231851-52fb6ea. Real
Chrome desktop upgrade payment and mobile downgrade/refund completed. Browser
initial orders are API-prepared; authentication uses a directly issued QA token,
not password login. Cross-month and all-resource fault matrices remain open;
do not treat this narrower regression as full browser trading completion.
Real payment/bank reconciliation needs external test data.

## Owner
wangjizhong, biru (reviews and merges PRs)

## How to build / run / test
```bash
python -m unittest discover -s backend/tests -q
cd frontend
node --test tests/*.test.mjs
npm run build
```

## Architecture
Vue frontend, FastAPI control plane, PostgreSQL, Redis and MinIO. Native APISIX
handles API traffic and quota; OAuth2/OIDC are issued by the platform. The
online delivery worker verifies resources before activating subscription terms.

## Conventions
<!-- Language/version, code style, naming, branch naming (wip/<name>/<topic>). -->

## Gotchas / known issues
Keep ORD-261012005144E1B3 and its temporary product dependencies. Runtime scripts
require private ownership manifests and precise FK/external-key cleanup.
Never bulk-delete production tasks, Redis counters or test-looking orders.
Initial API caller credentials are automatically issued on first verified
fulfillment. Download is restricted to verified buyer enterprise administrators,
not platform personnel. This requires the persistent market-api-credentials
Secret/API_CREDENTIAL_ENCRYPTION_KEY; never rotate it on deployment. Preserve
revoked and legacy hash-only keys rather than silently issuing replacements.
Ordinary refunds after version changes (TRD-BE-019) remain a separate unfinished
workflow; do not confuse them with simulated version-change funding refunds.
See docs/升降级Saga与月度清算接入详细设计-20261012.md for boundaries and tests.
