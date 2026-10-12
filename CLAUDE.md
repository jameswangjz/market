# OperationPlatform — project notes for humans and AI agents

Keep this file current. It is the first thing read when someone (or an agent)
picks up this repo. Update it in the same PR as any significant change.

## Purpose
Enterprise data/service marketplace with product review, paid fulfillment,
API/SaaS integration, settlement, auditing and platform support.

## Current status
2026-10-12: SaaS provider OAuth2/OIDC combined download and browser SSO are
deployed. API/model API automatic fulfillment is deployed as
20261012130931-e335854 in the existing online delivery worker, including its
APISIX Admin Secret reference. Runtime acceptance uses exact disposable fixtures;
review approval is a fixture precondition and payments are simulated. Do not
close TRD-BE-012 or full-flow QA solely on these narrower adapter checks.

Version-change Saga/economic adjustments and associated recovery remain
unfinished. Real payment/bank reconciliation needs external test data.

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
Initial API caller credentials are generated on demand in the order UI, not
automatically stored as a recoverable secret by the previous deployed worker.
The next release adds automatic initial issuance, encrypted storage and a
buyer-only order download. It requires the persistent market-api-credentials
Secret/API_CREDENTIAL_ENCRYPTION_KEY; never rotate it on deployment. Preserve
revoked and legacy hash-only keys rather than silently issuing replacements.
Version-change adjustment ledger code is internal groundwork only, not a
completed Saga, refund flow or monthly settlement integration. See
docs/初始调用凭据与升降级清算补偿设计-20261012.md for boundaries and tests.
