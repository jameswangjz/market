# R2 release provenance and isolated restore

After the parent freezes and commits all code, build the local frontend with
`npm ci` and `npm run build`. Select one UTC YYYYMMDDHHMMSS-GitSha tag and
use it for both preparation and deployment. Short Git SHAs remain supported.

```sh
python3 scripts/trading_artifact_gate.py prepare --repo . --commit "$GIT_SHA" \
  --tag "$IMAGE_TAG" --output /tmp/market-release
```

Preparation resolves the command commit, exports its regular tracked files,
checks working frontend source/public against the commit, independently builds
with npm ci, and compares all compiled asset hashes to the local dist. A stale
dist cannot pass by supplying a marker. The export receives a dynamic
`frontend/dist/market-build.json` after the build containing the actual full
commit, exact tag and compiled asset hashes. No tracked public marker is needed;
putting the commit's own SHA into tracked public would be circular.

The command emits the full commit, tag, and manifest SHA-256. Retain that digest
in the trusted parent pipeline, separately from the shipped release directory.
Tar and ship the prepared directory contents unchanged, without Git metadata
or node_modules. The source manifest covers the entire committed export; dist
has its own complete inventory. Remote verification hashes at most 50,000
files / 1 GiB per inventory, rejects symlinks, missing/extra files and mismatches,
and checks the compiled marker against the command release and asset manifest.

```sh
python3 scripts/trading_artifact_gate.py verify --repo /path/to/stage \
  --commit "$GIT_SHA" --tag "$IMAGE_TAG" --manifest-sha256 "$PROVENANCE_SHA256"
```

The deployment script requires these same three environment values and verifies
before cluster access. It consumes the checked dist in both image build modes;
it does not invoke Git or npm on the VM. The digest must come from preparation,
not be recomputed from the received manifest. This is pipeline integrity evidence,
not a signed image attestation or proof of the running image. Keep the staged
directory frozen while images are built. No gate command deploys anything.

```sh
python3 scripts/trading_rollback_rehearsal.py --backup /path/to/backup.sql \
  --image postgres:16-alpine \
  --engine-argv '["nerdctl", "--namespace", "k8s.io"]'
```

The VM image defaults to postgres:16-alpine. Fully qualified references and
postgres@sha256 digests are accepted; pinned digests are preferable. The engine
is explicit argv (Docker may use `["docker"]`), never shell text. The image
must already be available. A unique disposable container has no network,
published ports or host mounts. The helper streams a trusted plain pg_dump
backup into psql twice, fails on SQL errors, compares streamed snapshots,
checks the backup remained unchanged, removes the container/anonymous volumes,
then lists containers to verify its exact name is absent. Engine listing errors
and cleanup failures prevent a success claim. Other backup roles/extensions
besides the created market role must be available or restore fails.

An optional `--compatibility-hook-argv '["/path/to/probe", "arguments"]'`
runs caller-owned old/new application compatibility assertions against each
restored database. It receives REHEARSAL_CONTAINER, REHEARSAL_DATABASE and
REHEARSAL_ENGINE_ARGV. The hook must target only that disposable container;
it is trusted executable code and owns its business assertions and cleanup.
Hook success is reported separately from restore repeatability. Without the
hook, no application rollback claim is made. Stopping the new health schedule,
preserving added schema/audit records and business invariants remain separate
parent integration work. No live restore, task update or commit is performed.

## Restored read rollback compatibility hook

```sh
python3 scripts/trading_rollback_rehearsal.py --backup /path/to/backup.sql \
  --compatibility-hook-argv '["python3", "scripts/trading_rollback_compatibility.py", "--old-image", "market-api:OLD_TIMESTAMP_SHA", "--new-image", "market-api:NEW_TIMESTAMP_SHA"]'
```

The hook inspects the helper-owned database container's network-none mode,
ownership label and running state. It resolves already available API images
to immutable local IDs and runs new then old in separate ephemeral containers
sharing only that isolated network namespace. It publishes no ports and mounts
no host files. Probe code arrives over stdin. DATABASE_URL targets the restored
market database on localhost with trust authentication; background messages
are disabled. TestClient is constructed without a lifespan context: application
startup, task seeds, workers, gateway changes and startup repair never run.

Each image must read existing restored finance rows and provider-owned
settlements. Seven shared GET probes check finance/provider lists, buyer-mode
and platform-finance denial for the provider, anonymous denial and public
category rows. Insufficient actors, owned settlements or public rows
fail closed. PostgreSQL sessions are set read-only. Protected full-row hashes
cover orders, products, versions, payments, settlements, lines, batches,
proposals and audit records before/after the reads. The images must agree on
finance/provider projections, public categories and protected fingerprints.
Only hashes/counts/status metadata are emitted; bodies, identifiers, credentials
and exception details are suppressed. Each ephemeral API container is removed
and its absence checked even on probe failure.

Optional `--ensure-category-schema` on the hook calls only the new image's
category schema helper before read-only probes. It is reported separately;
it never invokes full startup or seeds categories. Protected business rows
must remain identical across this schema preparation and both image reads.
Optional `--new-detail-checks` runs two additional own-detail/foreign-detail
authorization checks on the new image only, reported separately as new feature
capability. The old 01cd96c image need not expose the BE008 detail route. Foreign
settlement rows are required only for these optional checks. By default missing
rows fail closed. The explicitly authorized isolated fixture mode below supplies
synthetic rows only inside the disposable restored copy.
This proves restored database read rollback compatibility for these GET routes,
not worker behavior, economic writes, gateway compatibility or full production
rollback. The parent reported a successful actual VM double restore; its hashes
and explicit scope are recorded in
`docs/evidence/trading-r2-ops-isolated-restore-20261013.json`.

## Optional Disposable Fixtures

The old backup lacks a verified provider actor linked to a settlement. Use the
explicit opt-in for this isolated rehearsal, with the already available images:

```sh
python3 scripts/trading_rollback_rehearsal.py --backup /path/to/backup.sql \
  --compatibility-hook-argv '["python3", "scripts/trading_rollback_compatibility.py", "--old-image", "market-api:20261013101927-01cd96c", "--new-image", "market-api:20261014025638-9570a05", "--seed-isolated-fixtures", "--new-detail-checks"]'
```

The host runner must first pass the existing inspect and kernel namespace,
interface/tunnel, route and published-port checks. Only then does it pass the
isolation authorization to the probe. Seeding also requires the exact localhost
DATABASE_URL, disabled background messages, fixture mode and new-image role.
The old-image probe receives fixture mode but never permission to seed.

The new probe inserts deterministic `r2-rollback-` identities: finance, provider
and buyer users, two enterprises and memberships, a category pair, two products,
two paid orders and owned/foreign settlements. Each settlement has gross/net
100, cost 40, profit 60, and five-party amounts 12/30/6/6/6. Collision with any
planned primary ID or fixture unique identity fails closed; prefix matching
never authorizes overwrites. No existing rows are updated or deleted. Only a
missing category table and nullable products.category_id column are created;
full startup, task seeding and general migrations never run.

Original protected business, actor, enterprise, membership and existing category
rows are fingerprinted before any application import. After insertion, the
same original column projection is compared, excluding only the exact fixture
IDs in their exact tables. This permits an additive nullable category column
without hiding changes to original values. The full post-seed fingerprint,
including fixture rows and new columns, is compared before/after each image's
GET probes and between images. The original projection is passed to the old
probe as column-name metadata; no row data or secrets are passed or logged.
Reports explicitly include `synthetic_fixture: true`, `original_unchanged: true`
and original/full hashes. These synthetic reads test restored schema and API
compatibility, not historical provider activity or full production rollback.
The backup and live business remain untouched; the enclosing helper drops the
disposable restored database after each rehearsal round.
