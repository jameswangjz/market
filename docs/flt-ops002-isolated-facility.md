# FLT-OPS002 isolated fixture facility

This facility does not alter routes, shared mock services, deployments, orders,
or products. The HTTP helper is a synthetic fault endpoint, not a forwarding
proxy. It has no outbound network capability and cannot attest real upstream
recovery. Nothing here has been run against the live environment.

## Exact saga queue selection

Copy the shape of `scripts/fixtures/flt-ops002-scope.example.json` into a private
manifest using the existing acceptance fixture journal's exact IDs. Set its
permissions to `0600`; loading requires current-user ownership, a regular file,
one hard link, and no symlink in either the file or its parent directories.
Files larger than 4096 bytes or changed during reading are rejected. Workers
load an immutable scope snapshot once at startup. The example
is a template, not authorization to claim any live row. Only `qa-vcs-<12 hex>`
API/model fixtures are supported. Unknown fields, empty scopes, wildcard IDs,
other products, and the protected order are rejected. Before leasing, the worker
checks the exact request, source order, buyer enterprise, combination, product,
the source order's subscription term in the combination's current generation,
and absence of a protected-order product collision in the database. The request
model has no `product_id`; its product is bound through the source order and
combination. The same predicate is checked again before processing the lease.

Parent integration must configure **every shared saga worker** with
`VERSION_CHANGE_EXCLUDED_SCOPE_FILES=/private/exact-scope.json` before releasing
the fixture request. Multiple manifest paths are comma-separated. Shared workers
exclude exact request IDs even if ownership metadata has drifted. Restart workers
to reload manifests; there is no dynamic queue registration. Configure the
isolated worker with `VERSION_CHANGE_FIXTURE_SCOPE_FILE=/private/exact-scope.json`
and launch `python -m app.version_change_worker` from the backend environment.
Selection and exclusion together fail at startup. Missing/malformed configured
files fail at startup. With neither variable, existing shared selection persists.
The SQL lease and retry timing remain the queue; no retry acceleration is added.
The isolated worker uses its own run/request heartbeat path under `/tmp`.

These optional settings alone do not isolate an already running shared worker.
Do not release a live fixture until parent validates exclusion in all competing
workers. No deployment edits or restarts were performed for this implementation.
There is no atomic reservation registry or registration lock in this partial
facility. A standalone worker cannot establish exclusivity by loading its own
manifest. Parent must either supply such registration to every scheduler or
validate the static exclusions while the exact fixture remains unreleased.

## Authenticated local fault endpoint

Set `QA_FIXTURE_FAULT_SECRET` to a dedicated random secret of at least 32 characters
using the process environment, then run:

```sh
.venv/bin/python scripts/isolated_fixture_fault.py --manifest /private/exact-scope.json --mode unavailable
```

The helper prints its ephemeral loopback URL. GET that exact URL with
`Authorization: Bearer <secret>`. `healthy` returns synthetic HTTP 200;
`unavailable` returns 503; `timeout --delay 15` waits 15 seconds then returns 503.
Delay is capped at 30 seconds. Unauthenticated requests get 401; other paths,
queries, and absolute proxy URLs get 404. Other methods are unsupported. There is
no control API, upstream configuration, forwarding, request-body handling, or
secret access log. Stop/restart this dedicated process to change the mode.
The secret is not stored in the fixture manifest. This endpoint is local only;
container or Kubernetes wiring is intentionally left to parent integration.

## Uncovered stages

- Online delivery workers and application-health workers do not yet support this queue scope.
- SaaS fixtures use a different ownership shape and are deliberately rejected.
- No real APISIX/OAuth/SaaS forwarding, gateway authentication injection, Redis,
  payment/refund upstream faults, or database crash facility is supplied.
- No live worker restart/recovery, retry exhaustion, compensation, quota
  continuity, or protected-order fingerprint acceptance has been executed.
- Parent must validate funding/plan/external-resource ownership using existing
  acceptance preflight before runtime use; source selection is not a sandbox for
  an externally corrupted funding ID or resource descriptor.

Focused local verification:

```sh
PYTHONPATH=backend:backend/tests python -m unittest test_fixture_faults test_version_change_saga -q
```

Validated locally: 16 tests passed using `/tmp/market-flt-ops002-tests/bin/python`
with FastAPI, SQLAlchemy, and python-multipart installed. The repository `.venv`
does not contain these dependencies. All test database writes used in-memory
SQLite and all HTTP requests used ephemeral loopback listeners. A subprocess
test launches the actual helper daemon, authenticates an exact fixture fault
request, checks its 503 response and fixture binding, and terminates the process.
