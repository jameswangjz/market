# Fixture Worker Final Integration Review

Review scope: `fixture_faults.py`, `version_change_worker.py`, their frozen
integration/dependencies, existing worker deployment defaults, and the parent
evidence. The final review found the registry activation race described below.
The parent authorized a minimal worker fix and regression test. The worker now
refreshes registry presence after candidate locking when a shared cycle initially
observed absence. Dedicated checks are unchanged. No deployment, commit,
task-state, or original frozen archive bytes were changed by this fix.

## Activation Race Finding

Before the fix, the shared worker cached `registry_present` at the beginning of its claim
transaction ([worker line 44](../backend/app/version_change_worker.py#L44)). If it
observes an absent table, then another transaction provisions the registry and
committed the first reservation before selection, both selection and post-lock
recheck retained empty registry filters. The shared worker could claim and prepare
that newly reserved fixture. Request leases still prevent simultaneous processing,
but shared-worker exclusion is violated during this activation window.

A deterministic local SQLite reproduction returned cached absence after creating
the table and committing a reservation, then invoked the actual shared claim/
prepare path. Result: reserved fixture processed, one prepare call. This is a
Python cached-state race applicable to PostgreSQL's fresh statements too; a real
concurrent PostgreSQL activation test was not run. Earlier core/matrix tests
provisioned before worker cycles and did not cover this transition. Their passing
results remain valid for their tested conditions.

The authorized fix adds a registry existence refresh and exclusion after locking
the candidate, before the existing fresh post-lock SQL statement and durable
lease. Acquisition already locks that request, so a new reservation cannot commit
during this locked check/lease interval. When the registry remains absent,
ordinary processing continues; existing-registry and dedicated checks are
unchanged. There is no global pause.

The deterministic regression is
`test_fixture_reservations.ReservationTests.test_registry_activation_before_selection_excludes_reserved_fixture`.
Before the fix: one test failed with `AssertionError: True is not false`.
After the fix: one test passed, verifying skip, no prepare call, no lease/retry/
plan mutation, retained reservation, and catalog observations `[False, True]`.
The focused facility suite ran 27 tests successfully, with ten PostgreSQL tests
skipped without the private URL: seventeen local tests passed.

The inherited private PostgreSQL counterpart is
`test_fixture_reservations.PostgreSQLReservationTests.test_registry_activation_before_selection_excludes_reserved_fixture`.
It requires the parent's pod-local URL and has not been executed by this agent.
The earlier 36-case result is baseline domain evidence, not evidence for this
new activation regression or deployed fix. No full-matrix rerun was performed.

Keep live reservation activation disabled until every claimant has the refreshed
code and the parent verifies the PostgreSQL regression. Provisioning an empty
registry before claimant startup remains a safe gate for older builds.

## Integration Conclusions

| State | Reviewed Behavior |
| --- | --- |
| Registry absent, default env | Ordinary selection and request leases remain. No table creation or proxy occurs; the normal heartbeat path remains. There is an additional catalog lookup and post-lock recheck. |
| Registry present but empty | NOT EXISTS exclusion filters out no requests; ordinary workers continue. |
| Reserved fixture | Shared workers exclude matching change, source order, or combination, including expired entries. Unrelated requests remain eligible. Product uniqueness governs reservation acquisition; it does not pause every request for the product. |
| Dedicated worker | Exact change/order/product/enterprise/combination/current term generation must match live data. Matching hashed token and unexpired reservation are checked before durable claim and again before phase processing. |
| Competing owner or cleanup | Request-then-reservation row lock order and unique constraints serialize acquisition. Wrong owner cannot renew/release; outstanding leases block release and expired recovery. |
| Crash or expiry | Request row lock prevents concurrent phase processing. Expired reservations stay quarantined. Crash lease reconciliation requires exact external evidence; expiry never hands a fixture back automatically. |

The implementation evidence is in
[claim/phase checks](../backend/app/version_change_worker.py#L44),
[shared exclusion](../backend/app/fixture_faults.py#L117),
[reservation/cleanup locking](../backend/app/fixture_faults.py#L160), and
[default configuration/heartbeat](../backend/app/version_change_worker.py#L268).
There is no global pause flag, global queue operation, Redis flush, shared-service
kill, or automatic registry cleanup in these paths.

The parent reported 25 passing actual PostgreSQL core tests and its subprocess
guard evidence. The inspected [full matrix JSON](evidence/full-worker-fault-matrix-20261014.json)
records 36 passing cases, 210 cycles, real retry scheduling, protected private
sentinels unchanged, no original table use, and cleanup of
`qa_fixture_matrix_cb08cf71e0c34718bc60929986c90a67`. Summed case duration was
1061.58 seconds. These results prove private domain/HTTP-contract integration;
they do not establish rollout of this code or native vendor transport behavior.
Earlier native acceptance evidence remains a separate evidence set.

## Model Semantics

Model-serving products and new model-serving smoke fixtures are canonical `api`.
No new `model_api` product category or live legacy-model fixture should be
registered. The frozen matrix selector/test/evidence named `model_api` verifies
only historical resource-adapter compatibility. Existing `-p-model` manifest
acceptance preserves exact legacy ownership; deployed smoke uses `-p-api`.

## Proposed Deployment Smoke

For deployed guard verification, preserve the harness's repository layout:
place `fixture_reservation_daemon_check.py` and `isolated_fixture_fault.py` in
`/tmp/deployed-fixture/scripts`, with `/tmp/deployed-fixture/backend` symlinked
to `/app`. The harness constructs child PYTHONPATH from that root, so this loads
the actual installed app source without a bundle override. A direct copy to
`/tmp` incorrectly produces `/backend:/scripts` and fails the first child before
any guard executes. That layout failure is not an application-core failure;
schema cleanup still runs. Keep DATABASE_URL derivation entirely inside the pod.

This is a plan for the parent after an approved rollout; no deployment was
performed during the review. Do not deploy the private test bundle as the API
application or point private runtime mocks at native vendor endpoints.

1. Pin the tested application image and verify it contains both updated worker
   and `fixture_faults` modules plus their domain dependencies. Run the bundled
   Python 3.12 dependency preflight in the deployed interpreter. Inspect effective
   default environment names without printing their values: selection, explicit
   exclusions, and fixture tokens must be absent from ordinary shared workers.
   Confirm READ COMMITTED and the intended registry schema/search_path and role.
2. Use the existing worker RollingUpdate configuration (`maxUnavailable=0`,
   `maxSurge=1`) without a global pause or scaling to zero. Wait until every
   request claimant uses the updated image/code before creating any reservation.
   Mixed-version claimants cannot safely honor reservations. Builds predating the
   activation refresh must also start after empty-registry provisioning, or their
   pre-provision cycles must be proven finished before activation. Require the
   refreshed build and passing parent PostgreSQL regression before activation.
   With registry absent
   where applicable, observe two advancing normal heartbeat samples and ordinary
   processing; startup must not create a registry or fault endpoint. If the table
   already exists, retain it and inspect it rather than dropping it for this test.
3. Before registry activation, explicitly provision or verify only its table,
   expected fields/constraints, and permissions. Shared worker roles need SELECT;
   controller/dedicated roles need the operations and row locking used by their
   helpers. Do not rely on existence checks to establish table readability. A
   registry lookup/permission error fails closed and can prevent ordinary worker
   cycles, so resolve the role/schema mismatch before activating reservations.
   Verify ordinary heartbeat/queue progression with an empty registry.
4. In the approved QA environment, create two exact-owned canonical API canaries
   with distinct fixture roots/products. Reserve canary A atomically before making
   it eligible; if acquisition sees an outstanding lease, abort fault activation
   and reconcile that exact canary. Keep its controller token private and manifest
   owned/0600. Leave canary B unreserved. Shared workers must advance B while A
   remains unchanged, proving isolation without a queue pause. Check protected
   order/product fingerprints before/after; never mutate them or use them as
   canaries. If they have competing active work, postpone the canary smoke.
5. Run a bounded dedicated process on A with a wrong token; no A lease, phase,
   ledger, funds, or resource mutation is acceptable. Stop only that process.
   With no outstanding A lease, allow a short A reservation to expire while
   holding the canary idle. Shared workers must still exclude A; the matching
   token must not authorize expired dedicated work. Renew with the matching owner
   before continuing. Never delete a reservation or clear a lease just to make
   a failed guard pass.
6. Run the correct-token dedicated process against A's approved healthy QA
   adapter. Verify exact request completion, proof/ledger identity, expected
   revision and fund/refund counts, unchanged original snapshots, and B's ordinary
   progression. Use the scoped heartbeat
   `/tmp/<run_id>-<change_id>-heartbeat` for any dedicated probe: the shared
   deployment's default `/tmp/version-change-heartbeat` probe is not appropriate
   for a scoped worker. Do not inject loopback URLs into native gateway configs
   or weaken production DNS/SSRF/OAuth validation to run this smoke.
7. Keep HTTP fault/crash injection in the existing private-schema harness using
   the deployed code artifact and local vendor contracts. If a rerun is needed,
   use actual PostgreSQL/retry mode; never mix `--local-smoke` evidence into deployed
   results. Real vendor faults, if separately authorized, must be exact QA
   resources with existing ownership/restore procedures, never shared-service kills.
8. Stop only owned dedicated/proxy processes, validate no outstanding fixture
   lease and the intended external state, then release A with its matching owner.
   Verify no leftover reservation for either canary, standard heartbeat and queue
   progression, unchanged protected fingerprints, and no unexpected phase errors.
   Capture image/code identities and named smoke results without tokens or URLs.

Do not roll back to older claimants while any live or expired reservation remains.
On a failed smoke, stop creating fixtures and stop only owned dedicated processes;
retain updated shared claimants and quarantine until exact reconciliation permits
cleanup. Do not drop the registry, delete quarantines wholesale, flush queues,
or clear crash leases based on time alone. Once no active/quarantined fixture or
outstanding lease remains, the parent can assess an ordinary image rollback.

## Remaining Limits

The deployed smoke above has not been executed by this review. Runtime inspection
adds database round trips, and registry availability/permissions are operational
dependencies once it exists. Native APISIX/Lua/Redis, provider OAuth/payment
transport, and continuous deployed worker scheduling remain outside the private
matrix claim. The activation refresh is implemented and awaits parent PostgreSQL
verification and rollout. No live execution or deployment was performed here.
