# Unified Delivery Access Acceptance

## Scope and execution status

Validation-only harness: `scripts/unified_delivery_access_acceptance.py`.
Main-agent integration executed the real PostgreSQL/network HTTP contract on
2026-10-12: 104 cases passed on deployment 20261012043020-fa0efa1. Exact database
and synthetic Redis hash cleanup passed. See the public artifacts directory and
the Chinese validation record for execution evidence. Retained or real user orders
are never adopted. Guard tests are independent of application developer tests.

The future live run uses already migrated PostgreSQL and a separately running
API with matching code, database, token signing settings, and wall clock. It sends
network HTTP requests with redirects and environment proxies disabled. It never
starts the API or enters its lifespan. No TestClient or HTTP mocks are used.

## Private ownership

Prepare generates a fresh `qa-uda-<12 hex>` scope. Supply a new absolute manifest
path on a private POSIX filesystem. The manifest is created exclusively at 0600
before mutations and retained through cleanup. It contains exact owned IDs, phase,
and discovered PKs, never passwords or tokens. Tokens exist in process memory only.

All enterprises and users are new, verified and active; users have verified email.
Products remain DRAFT. No storefront purchase, publishing, payment transition,
worker task, gateway publish, SaaS provision, or object upload is performed.
Payment snapshots, subscription calendar terms, clean scan metadata, active routes,
and the nonempty consumer marker are explicitly synthetic fixtures. They prove no
bank payment, scanner, file storage, APISIX synchronization, or upstream availability.

Cleanup follows exact owned roots and FK children using the existing guarded helper.
Additional checks reject unexpected orders, files, routes, credentials, public
products, and children referencing external users/enterprises/products. Unexpected
business linkage makes cleanup fail closed; retain the manifest for investigation.
It never deletes by prefix/LIKE, lists storage, or adopts real user rows. The protected
retained order `ORD-261012005144E1B3`, product `productqa-vcq-a044ea9ea181-p2`, and
their related data are excluded. Do not edit a manifest to claim another scope.

Run modes sequentially with no concurrent browser, worker, or acceptance activity
on these fixtures. Interrupted scenario changes can remain in owned rows; cleanup
is the recovery action. Prepare does not silently adopt collision rows or clean
them on failure. An allocated manifest permits explicit cleanup after inspection.

## Commands for a later authorized run

Keep `version_change_quote_acceptance.py` alongside the new script. Use the existing
API environment and a private PostgreSQL connection setting; do not put secrets in
command arguments, transcripts, public reports, or this runbook. Use a POSIX runtime
where mode 0600 is enforced. These commands have NOT been executed against a VM.

```bash
PYTHONPATH=backend:scripts .venv/bin/python scripts/unified_delivery_access_acceptance.py --mode prepare --manifest /tmp/uda-new-owned-run.json
PYTHONPATH=backend:scripts .venv/bin/python scripts/unified_delivery_access_acceptance.py --mode test --manifest /tmp/uda-new-owned-run.json --base-url http://192.168.10.10:30080 --gateway-origin http://192.168.10.10:30082
PYTHONPATH=backend:scripts .venv/bin/python scripts/unified_delivery_access_acceptance.py --mode cleanup --manifest /tmp/uda-new-owned-run.json
```

Use a fresh manifest filename for every prepare, even after successful cleanup.
API `API_GATEWAY_PUBLIC_URL` must already be `http://192.168.10.10:30082` for the
normal configuration above. Platform nginx on 30080 does not proxy `/gateway`.
The harness asserts the full gateway DTO address on 30082 but never invokes it.

For an API already configured with missing/invalid `API_GATEWAY_PUBLIC_URL`, add
`--expect-gateway-unavailable` to test. API/model responses must be not ready, have
empty `gateway_url`, and retain credential management access. This harness cannot
change the environment of a running API. Do not redeploy merely to run this mode
without separate authorization. Configuration setup belongs to the application/
deployment owner; this change only supplies validation artifacts.

Save stdout JSON as public proof only after inspecting it. It includes authored
assertions and statuses, never raw exceptions, SQL, HTTP response bodies, tokens,
credential hashes, upstream URLs, or database connection strings. A test pass
does not imply cleanup: only a separate successful cleanup report verifies exact
owned rows and FK children absent. Retain the private manifest as ownership proof.

## Covered and deferred

Normal file metadata and API/model access allow buyer super/admin roles; member,
cross-enterprise, provider and platform roles are denied for GET and POST. GET
compares all owned persisted rows, including orders, routes, credentials, terms,
and audit rows, before and after each request. Failed POST requests also must leave
these rows unchanged. Responses forbid sensitive fields/known synthetic values.

The old paid order uses a different version from the current paid subscription.
The DTO must select the current route, expiry and quotas, including `quota_amount`.
Active wrong-version routes cannot be selected. Calendar-consistent expired/future
entitlements, disabled routes, invalid route slugs, missing consumer marker,
disabled/expired/order-scoped credentials, unpaid/part-paid/unfulfilled/refunded/
cancelled/historical orders, and security-unpublished products block access.
No usable shared credential must retain the configuration path.

Snapshot comparisons exclude exactly `notification_deliveries`,
`notification_delivery_attempts`, and `notification_recipients`, whose identities
and transport state can change under the independently running notification worker.
Asynchronous rows in these three tables are not measured for GET side effects or
scenario restoration. No prefix-wide exclusion is used. Message/business events,
expired-urgent records, preferences, attachments, attachment links, audit, orders,
terms, routes, and credentials remain measured. Ownership discovery, manifest PK
capture, and exact FK cleanup include all three excluded tables. No global worker
is disabled or modified. Public reports list the exclusion.

SaaS is NEGATIVE ONLY: missing OIDC client, owner/product mismatch, and malformed
application URLs reject GET readiness and POST launch. URL cases are not isolated
URL-validator proof because absent OIDC configuration can reject first. No positive
SaaS launch, historical SaaS version compatibility, URL navigation, or launch audit
is claimed. A future positive run requires real RSA environment and live-provider
client/term fixtures following `backend/tests/test_oidc_provider.py`; it must not
assume that absent OIDC configuration can be positive.

File access checks frozen metadata and unused quota, not downloading or real scan/
MinIO proof. Current/future checks verify entitlement selection and blocking, not
delivery provisioning. Database snapshots detect persisted effects on owned rows;
they do not instrument an external API process to prove absence of transient
gateway calls. No browser/CDP proof or four-delivery E2E completion is claimed.

## Local guard verification

```bash
PYTHONPATH=scripts /tmp/market-business-event-venv/bin/python -m unittest discover -s backend/tests -p test_unified_delivery_acceptance.py -v
```

Local verification: sixteen guard tests passed with this existing Python environment.
The repository `.venv` and `/tmp/market-quote-local-venv` lack SQLAlchemy, so they
cannot run the FK safety test until dependencies are installed. CLI help and
`git diff --check` also passed. Main-agent real-PG/network-HTTP execution is recorded above.

The periodic scan uses advisory transaction lock 72810421 and refreshes credential
policies. Live test/cleanup hold that existing lease and release it on session
close; delivery workers remain running. Only notification_deliveries,
notification_delivery_attempts and notification_recipients are excluded from
equal-snapshot assertions. Ownership and exact FK cleanup still include them;
core business/audit/credential rows remain measured. Cleanup atomically checks
the exact synthetic consumer metadata hash's subscription scope before removal.
No prefix scanning, actual APISIX route or real consumer mutation is performed.

These tests validate private manifests, protected-scope rejection before application
import, lifecycle and collision checks, secret-safe errors, public origin validation,
synthetic economic fields, and refusal of external user linkage using an isolated
in-memory SQLite schema. SQLite is only for safety guards, never live acceptance.
