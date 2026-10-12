# OperationPlatform — project notes for humans and AI agents

Keep this file current. It is the first thing read when someone (or an agent)
picks up this repo. Update it in the same PR as any significant change.

## Purpose
Enterprise data/service marketplace with product review, paid fulfillment,
API/SaaS integration, settlement, auditing and platform support.

## Current status
2026-10-12: SaaS provider OAuth2/OIDC combined download and browser SSO are
deployed. API/model API automatic fulfillment is being added to the existing
online delivery worker. Runtime acceptance uses exact disposable fixtures;
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
automatically stored as a recoverable secret by the fulfillment worker.
