# Mock SaaS OIDC RP

This development RP preserves `/health`, `/oauth/token` client-credentials and
`/isv.php` machine operations. OIDC user tokens are separate from machine tokens
and platform console authentication. It does not verify third-party SaaS or
production browser interoperability by itself.

## Environment

| Variable | Contract |
| --- | --- |
| `MOCK_SAAS_DB_PATH` | Default `/data/mock-saas.sqlite3`; mount a persistent volume at `/data`. Database mode is 0600. |
| `MOCK_SAAS_PUBLIC_BASE_URL` | Browser origin, e.g. `http://127.0.0.1:30082`. No path/query/credentials. |
| `OIDC_ISSUER` | Exact public OP origin, e.g. `http://192.168.10.10:30080`. |
| `OIDC_BACKCHANNEL_BASE_URL` | Trusted internal OP origin, default `http://market-api:8000`. Discovery, JWKS, token and userinfo use only this origin. |
| `OIDC_ALLOW_HTTP_DEVELOPMENT` | Must be explicitly `true` for HTTP public origins; otherwise HTTPS is required. |
| `MOCK_SAAS_ENABLE_TEST_ADMIN` | Only explicit `true` exposes fixture administration; default disabled. |
| `MOCK_SAAS_ADMIN_SECRET` | Required with enabled administration; supplied as `X-Mock-Admin-Secret`. |
| `PLATFORM_OAUTH_INTROSPECTION_URL` | Existing machine token introspection; default `http://market-api:8000/oauth/introspect`. |

The Docker command disables access logs so callback code/state query strings do
not enter access logs. Keep proxy query-string logging disabled too. Do not
enable HTTP wire/body debugging, expose the SQLite volume, or place it in a web
root. The volume persists client credentials and short-lived access tokens;
file permissions are not encryption or a substitute for protecting backups.

## Fixture Configuration

`POST /_test/oidc/clients` requires the admin header and this JSON body:

```json
{
  "client_id": "OP-issued-client-id",
  "client_secret": "OP-issued-one-time-secret",
  "redirect_uri": "http://127.0.0.1:30082/sso/callback",
  "product_id": "fixture-product-id",
  "issuer": "http://192.168.10.10:30080",
  "product_name": "Application"
}
```

`redirect_uri` must equal `${MOCK_SAAS_PUBLIC_BASE_URL}/sso/callback` and `issuer`
must equal `OIDC_ISSUER`. Success is 201 and never includes the secret; duplicate
client IDs return 409 instead of silently rotating credentials. There is no
configuration GET endpoint. `DELETE /_test/oidc/clients/{client_id}` deletes only
that client and its flows, sessions and OIDC mappings. Shared machine tenant and
user records are not removed by client deletion. CLOSE the scoped tenant using
the authenticated machine interface as part of fixture teardown.

Before login, `/isv.php` must actually receive authenticated `OPEN` with
`product_id`, `subscription_id`, `enterprise_id` and optionally
`enterprise_name`/`tenant_name`. Preserve the returned `tenant_id` in the OP
subscription. Existing machine introspection remains required; an RP client
configuration alone never opens a tenant. `CLOSE` blocks local login and existing
sessions immediately. `CHANGE`/`RENEW` cannot replace the required OPEN.

Machine introspection must report `active: true`, the owning `product_id` and a
space-separated `scope`. OPEN/CLOSE/RENEW/CHANGE require `saas.tenant`;
USER_ASSIGN/USER_UNASSIGN require `saas.user`; DEPT_CREATE/DEPT_REMOVE require
`saas.department`. `resource.invoke` is not a SaaS management permission.
New-tenant OPEN requires all three identity fields. Existing-tenant OPEN may
omit them and restores from the complete immutable persisted identity after
checking token product ownership; it cannot rebind product, enterprise or
subscription. Non-OPEN calls may omit `product_id` only
when the referenced tenant already exists and belongs to the token's product.
Any supplied product/enterprise/subscription must agree with the persisted
identity. These ownership checks and mutations share one SQLite write transaction.
RENEW persists its returned status/expiry but never reopens a CLOSED login gate.
The platform's machine token currently binds only the product; enterprise and
subscription are frozen by the first authorized OPEN, not independent OP claims.
USER_UNASSIGN resolves platform or external user IDs to the same persisted row;
repeated external-ID revocation retains that ID and creates no extra user row.

## Browser Flow

- Entry: `/sso/apps/{client_id}?enterprise_id={enterprise_id}`.
- Callback: `/sso/callback?code=...&state=...`.
- Application: `/app`; checks OP userinfo on every request.
- Logout: `POST /sso/logout`; deletes only the local RP session.

The RP creates server-side state/nonce/verifier, a five-minute single-use flow,
and an independent HttpOnly/SameSite=Lax flow cookie. Authlib generates S256 PKCE
and exchanges with Basic client authentication. ID tokens require RS256, the
exact configured issuer, this client's audience, expiry, nonce and at_hash.
Trusted backchannel metadata must advertise exactly the public OP endpoints;
JWT `jku`/`x5u` is never followed. Userinfo sub/enterprise/subscription/tenant must
match the verified token, local OPEN and any existing mapping. First SSO can
create a mapping without USER_ASSIGN; email alone never establishes identity.

RP sessions have an independent HttpOnly cookie, are persisted in SQLite and
expire no later than the token, ID token or 300 seconds. OP rejection, local
USER_UNASSIGN or CLOSED tenant denies access. Public HTTPS enables Secure cookies.
If using the localhost development URL, forward browser localhost:30082 to the
VM NodePort; the OP issuer must remain reachable at its registered public URL.

## Local Verification

`backend/tests/test_mock_saas_oidc.py` uses a mocked HTTP OP and real RSA signing,
including negative claims, state/cookie/replay, restart persistence and exact
client teardown checks. These results are not deployed/browser evidence.

Protocol implementation follows the Authlib 1.6 client and OIDC claim APIs:
[OAuth client / PKCE](https://docs.authlib.org/en/v1.3.0/client/oauth2.html) and
[OIDC client claims](https://docs.authlib.org/en/v1.1.0/client/oauth2.html).
