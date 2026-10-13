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

Additional bounded acceptance: real Chrome password login and first purchase
through storefront, simulated payment, online worker fulfillment and buyer
credential summary passed on desktop/mobile, without token injection or preset
orders. Controlled February/March/April ledger dates passed production HTTP
monthly accounting in a newly created/dropped private PostgreSQL schema.
Model API owned-route deletion and mock SaaS owned-tenant closure passed actual
worker retries, resource recovery and simulated funding compensation. SaaS
target and compensation each exhausted three retries; finance HTTP retry and
its audit were verified after restoring the owned tenant. All owned fixtures
were removed, protected business data unchanged, worker batch restored to 10.
See docs/evidence/version-change-extended-regression-20261012.json and the fault
matrix for scope. Full resource network/token failure timing and real cross-month
subscription activation remain open; real bank payments are user-deferred.

## Owner
Available fault matrix follow-up: 66 bounded checks completed against deployed
PG/APISIX/Redis/mock SaaS. PG statement-error rollback, proof rejection, finance
HTTP retry audit, concurrency and real 61s lease crash recovery passed. API/model
adapter credential drift, partial writes and four-request counter continuity
passed. Native APISIX 401/502/500/504 and TTL recovery verified. SaaS auth and
nine locally corrupted post-CHANGE STATUS responses rejected and restored.
Distinguish actual component HTTP from in-process wrappers and controlled HTTP
external adapters; full daemon Saga timing and production HA are not closed.
Open finding OAUTH-CACHE-401: neither SaaS cache nor native OAuth cache evicts
on resource 401; failed credentials reject safely but recovery needs expiry or
targeted eviction. Do not mark auto recovery complete. See
docs/evidence/version-change-available-fault-matrix-20261013.json.

Machine OAuth revocation hardening: introspection now checks the live active
client, product/scope binding and a durable hash-only token revocation record.
Token issuance includes a unique jti; POST /oauth/revoke supports authenticated,
idempotent revocation without exposing foreign token ownership. Manual product
withdrawal still preserves sold access. Local backend regression: 750 tests,
44 skipped. API and all three backend workers deployed as
20261013001410-3626b03, with PG backup and credential Secret unchanged. Live
OAuth revoke/introspect plus mock-SaaS protected STATUS passed: individual
revocation preserves sibling tokens; client deactivation rejects all tokens
and new issuance. Exact generated fixture/audit cleanup passed. STATUS used an
absent tenant (authenticated 404 before revoke, 401 after), not fulfillment.
APISIX/cache paths and full Saga token-failure matrix remain open. Evidence:
docs/evidence/oauth-revocation-runtime-20261013.json.

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
