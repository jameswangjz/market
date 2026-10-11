# Product Registration Browser Acceptance

Real Windows Chrome read-only acceptance passed at desktop 1440x1000 and mobile
390x844 on deployment `20261010114829-840af84`, API pod
`market-api-765c75b875-pmph2`. Loaded assets were `index-C9st3nlF.js` and
`ConsoleApp-qlJEGKDY.js`. Main's final deployment `20261010115149-1b1845e`
hardens backend OAuth download permissions and retains the identical frontend
asset. Browser samples belong to the earlier recorded deployment; final cleanup
was confirmed on `market-api-57778d5566-7t9hv`.

Results and ten screenshots are in `20261010114829-840af84/`. Browser exit code
was 0. Both viewports passed field, identity, API and automated horizontal layout
checks. Registration and real integration panel screenshots were visually
inspected. One cosmetic limitation remains: at 390px, the "add version" button
text wraps into a narrow vertical stack. No control overlap or horizontal overflow
was detected. This is not a claim that all visual polish is complete.

## Evidence

- Real `/api/auth/me`: isolated verified provider user, selected owned enterprise,
  active super-admin membership, no platform role. Main fixed panel visibility
  through `canViewProductIntegration(user)` before the passing browser run.
- Registration: online/offline delivery groups, two versions with required
  code/price/cost/description, required per-version file inputs, required API
  URL/route/auth/scope/timeout/health/quota controls. Draft changes stayed local;
  no files were selected and no save or upload was attempted.
- Two real integration GETs: exact fixture version identity, all thirteen expected
  config fields, pending/healthy-false state, correct panel route/health text.
  Saved form values matched prices, costs, descriptions and quota units.
- Provider credential-download button available for `pending_integration`.
  Authenticated GET returned HTTP 200 and attachment metadata in both viewports.
  The body was canceled without reading or saving text; the credential download
  button was not clicked, and Chrome downloads were denied.
- One Verify POST per viewport was rejected locally before network dispatch.
  The UI displayed an error and retained pending integration status. This is not
  successful server verification or external provider health evidence.
- CDP-blocked mutations: 0; network mutation events: 0; native dialogs: 0.
- Cleanup verified owned rows and FK leftovers absent on the browser deployment
  pod and again on the final replacement pod. All private local and deployment-host
  state/token copies were removed.

FE004 remains partial for compatibility/OIDC callback support. This run covers
API integration, not SaaS integration or concurrent payment acceptance. Main
reports final backend 316 and frontend 80 tests passing; those tests are separate
from this browser evidence and were not rerun here.

## Fixture Lifecycle

Marker: `qa-reg-fe4bbf2467e2`. Preparation committed an isolated verified provider
user and enterprise, active super-admin membership, two API releases, validated
`save_config` routes and pending integration records, and a random placeholder
OAuth client. No usable external account was configured. Preparation ran on the
previous API pod; mode-0600 state/token copies were preserved outside that pod
before rollover. Cleanup state was restored privately on replacement pods.
No lifespan startup, migration, gateway publish, or health probe was invoked.

Cleanup validates exact `qa-reg-` ownership, refuses unexpected orders, versions
or memberships, reuses established message/audit scopes, locks owned outbox
parents, deletes in FK order and verifies owned rows and FK leftovers absent.
No prefix-wide deletion is used. Preparation/cleanup JSON files contain safe
markers/statuses only. No JWT, OAuth secret or SSH password is stored in repository
artifacts or screenshots. Nothing was committed by this worker.

## Reproduce

Use an already migrated API deployment and replace the pod name with the currently
ready pod. Copy scripts into `/tmp`, never `/app`.

```bash
kubectl -n market cp scripts/subscription_pg_concurrency.py market-api-57778d5566-7t9hv:/tmp/subscription_pg_concurrency.py
kubectl -n market cp scripts/product_registration_browser_fixture.py market-api-57778d5566-7t9hv:/tmp/product_registration_browser_fixture.py
kubectl -n market exec market-api-57778d5566-7t9hv -- env PYTHONPATH=/app python /tmp/product_registration_browser_fixture.py prepare --state-file /tmp/qa-reg-browser-state.json --token-file /tmp/qa-reg-browser-token.json
```

State/token paths must be distinct, absent and directly under `/tmp` or `/var/tmp`.
They are created exclusively with mode 0600. Transfer the token privately into a
Linux temporary file with mode 0600. Preserve cleanup state outside the pod before
any rollover. Never print the contents or transfer them into the repository.
Windows copies, if needed, require an owner-only ACL and removal after use.

```powershell
& C:\users\james\market\scripts\product_registration_browser_cdp.ps1 -FixtureFile '\\wsl.localhost\Ubuntu-26.04\tmp\qa-reg-browser-token.json' -DeploymentTag '20261010115149-1b1845e' -ExpectedConsoleAsset 'ConsoleApp-qlJEGKDY.js' -OutputDir 'C:\users\james\market\docs\product-registration-browser-20261011\20261010115149-1b1845e'
```

This invocation describes a future run against the final tag; retained screenshots
are from the earlier tag stated above. Always clean up after an attempt, including
failure. Retain private state until cleanup succeeds, then remove all copies.

```bash
kubectl -n market exec market-api-57778d5566-7t9hv -- env PYTHONPATH=/app python /tmp/product_registration_browser_fixture.py cleanup --state-file /tmp/qa-reg-browser-state.json --token-file /tmp/qa-reg-browser-token.json
```

Python AST, embedded JavaScript and Windows PowerShell parser checks passed.
Initial startup failures were a harness target-list enumeration issue corrected
before the passing run. They never reached application navigation or produced
screenshots. Final results replaced failed generated results; only final passing
screenshots/results are retained.
