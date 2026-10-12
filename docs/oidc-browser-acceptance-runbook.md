# Scoped Browser SSO Acceptance

This harness is ready for the browser SSO runtime batch. A `prepared` result is
not browser acceptance. Only the checked PowerShell artifact followed by RP and
PostgreSQL cleanup can produce checkpoint service evidence.

## Prerequisites

- Deployments and schema are managed by the main agent, outside this harness.
- Read the actual `market-api`, `market-web`, and `market-mock-saas` image tags
  with `kubectl get deployment -n market ... -o json`. Supply that actual batch
  tag as `--runtime-tag`; the argument does not independently attest an image.
- OP issuer: `http://192.168.10.10:30080`.
- RP issuer must equal the OP issuer; backchannel must reach
  `http://market-api:8000`; public origin must be `http://127.0.0.1:30082`.
- Mock RP must expose its protected configuration API, with
  `MOCK_SAAS_ENABLE_TEST_ADMIN=true` and `MOCK_SAAS_ADMIN_SECRET` from its secret
  environment. Inject the same existing secret into the preparation process.
  Never print it, paste it into shell arguments, or commit it.
- Mock service NodePort is **30083**. VM APISIX NodePort **30082** is untouched.
- Windows Chrome and WSL are required. The PS script owns a temporary TCP relay
  from Windows `127.0.0.1:30082` to VM `192.168.10.10:30083`. An occupied local
  port fails startup. `-NoProxy` uses an already established relay.
- Before Chrome starts, Windows must reach VM RP health with HTTP 200, see the
  loopback TCP listener, connect to it, and receive mock-saas health HTTP 200
  through it. These checks are recorded without response bodies. Chrome uses
  `--no-proxy-server`. A WSL-only listener is not sufficient for `-NoProxy`.

## Prepare

Copy `oidc_browser_acceptance.py` and the unchanged
`version_change_quote_acceptance.py` into `/tmp` in the API pod. Invoke with
`PYTHONPATH=/app` and the RP admin secret in the process environment:

```bash
python /tmp/oidc_browser_acceptance.py --prepare \
  --manifest /tmp/qa-oidc-browser.json --runtime-tag ACTUAL_BATCH_TAG
```

The helper constructs only a new synthetic SaaS product, owned enterprises,
users, paid/delivered orders and calendar terms. It gives users fresh random
login passwords and verified synthetic email addresses, obtains a real platform client-credentials token, checks its
introspection, performs real mock `OPEN`, and configures an independent OIDC
client through the protected RP endpoint. OPEN includes exact product,
subscription, enterprise and tenant identities. Machine scopes are
`saas.tenant saas.user saas.department`.

Three private 0600 files are created: the IDs ownership manifest, `.oidc.json`
RP ownership journal, and `.browser.json` login credentials. Preparation retains
fixtures on success. If an external mutation could have happened before a
failure, it retains the manifest/journal so cleanup can be retried safely.
An older retained fixture lacking verified email can be repaired with
`--repair-login --manifest ...`; this checks exact ownership and changes only
the five synthetic users' email-verification flags.

## Browser

Transfer `.browser.json` privately into WSL `/tmp` and preserve mode 0600.
Pass its original WSL absolute path; the script checks its mode before resolving
the Windows path. Use a fresh output directory on each attempt.

```powershell
& C:\Users\james\market\scripts\oidc_browser_cdp.ps1 `
  -FixtureFile /tmp/qa-oidc-browser.browser.json `
  -OutputDir C:\Users\james\AppData\Local\Temp\market-oidc-RUN
```

The actual login form and consent buttons are exercised at 1440x1000 and
390x844. Checks cover OP/RP HttpOnly SameSite=Lax cookies, owned identities on
the RP application, rejected consent without a session, cross-enterprise
denial, My Applications listing and member controls, launch through the UI,
and checkbox revocation invalidating an existing RP session and member listing.
Mobile is Chromium device emulation, not a physical device.

Only owned login/consent/launch/grant POSTs are allowed. Required console GETs
are allowed; no other mutation is allowed. CDP payloads, headers, cookies,
tokens, callback codes and credential values are never written to stdout or
artifacts. Screenshots exclude login forms and check password/token absence and
horizontal overflow. No unguarded failure screenshots are produced. Profiles
and the owned relay are removed in `finally`.

The output contains `browser-results.json` and five PNGs with SHA256 hashes.
Inspect all screenshots and the JSON before treating this as browser evidence.
The harness does not mark the task checkpoint complete by itself.

### Repeat After An RP Restart

The main agent can restart the mock deployment after the first passed browser
run, wait for readiness, and rerun the same retained fixture with
`-RestoreOwnedGrant` and a fresh output directory. Do not prepare again or run
cleanup between the two browser runs. The flag logs in as the owned buyer
administrator and restores only the fixture member's grant through its checkbox
before the complete browser checks. The rerun ends with that grant revoked again.

Since no client configuration or machine OPEN is repeated, successful RP login
after restart proves that the existing RP client configuration and tenant
identity survived on the PVC. It does not prove survival of an existing browser
session: the first run removes its private Chrome profile. Preserve both browser
artifacts and separate main-agent restart/readiness evidence; use the second
artifact for final cleanup evidence. The script does not restart deployments or
independently attest that a restart happened.

## Exact Cleanup And Evidence

Run cleanup even after browser failure. Copy the manifest and `.oidc.json`, both
0600, plus both Python scripts into `/tmp` in the mock pod. No backend import is
needed there:

```bash
python /tmp/oidc_browser_acceptance.py --cleanup-rp \
  --manifest /tmp/qa-oidc-browser.json --rp-store /data/mock-saas.sqlite3
```

This checks the configured SQLite path, owned client/product, exact tenant
identity, and session/mapping identities. It refuses unexpected users or
departments. It deletes only the exact client and tenant rows, checks client
FK cascades and tenant children, and marks the journal after commit. Copy that
updated journal back into the API pod, preserving 0600.
Missing journals and unverified RP cleanup both refuse PostgreSQL cleanup
before importing production or deleting rows.

After a failed browser run, clean PostgreSQL without passing evidence:

```bash
PYTHONPATH=/app python /tmp/oidc_browser_acceptance.py --cleanup \
  --manifest /tmp/qa-oidc-browser.json
```

After a successful, visually inspected browser run, copy the JSON and sibling
PNGs into a private artifact directory in the API pod and use:

```bash
PYTHONPATH=/app python /tmp/oidc_browser_acceptance.py --cleanup \
  --manifest /tmp/qa-oidc-browser.json \
  --browser-result /tmp/oidc-artifacts/browser-results.json \
  --evidence-output /tmp/oidc-service-evidence.json
```

Artifact validation requires matching run and runtime tag, all expected checks,
zero blocked requests, and matching PNG signatures/hashes. The helper then
verifies exact owned PostgreSQL rows and FK children absent, including browser
requests reached through their client FK. Only then does final evidence report
`status: passed`, `database: real PostgreSQL`, `cleanup: verified`, and passed
browser SSO, mock session, application access and negative cases. Preserve that
secret-free JSON and the reviewed screenshots. Remove every transferred login
credential copy; API cleanup removes only its own `.browser.json`.

Payments, delivery and SaaS subscription terms remain synthetic; the run does
not verify real payment, settlement, delivery workers or an external SaaS vendor.
No backend, frontend, mock source, commits, deployments or real data are changed
by the harness.
