# Mock SaaS External Identity Fix

New user and department identities are `mock-user-` or `mock-dept-` followed by
the full SHA256 digest of a JSON array containing the complete tenant ID and
platform user/department ID. The JSON framing prevents concatenation ambiguity;
the digest is stable across restarts and bounded below the 180-character identity
limit. Shared QA prefixes and the same platform ID in different tenants no longer
produce the same generated external ID.

Existing persisted external IDs, including SaaS-APP1 and old truncated IDs, are
preserved on reassignment, removal and reactivation. No migration or external-ID
rotation occurs. Old department rows lacking an external field retain the prior
truncated identity when recreated. Department creation returns its actual stored
external identity, including explicit/legacy IDs.

Explicit external IDs must be nonempty strings with no surrounding whitespace,
at most 180 characters. A different explicit ID for an existing platform identity
is rejected with 409. New identities cannot alias another platform or external
identity in the same tenant; explicit values remain scoped to their tenant.
Ambiguous preexisting external aliases reject removal with 409; exact platform-ID
removal still identifies the intended row. Existing colliding legacy IDs are not
automatically repaired or rotated.

Dedicated isolated verification:

```bash
python3 -m unittest discover -s backend/tests -p test_mock_saas_external_identity.py -v
```

Nine tests cover common prefixes, tenant separation, framed hashing, replay and
restart stability, targeted external removal, remove/reassign, legacy/SaaS-APP1
stability, explicit-field validation/alias guards, and ambiguous legacy removal.
Only temporary SQLite databases are used; no browser, Kubernetes, shared fixture
or application lifespan is executed.

Parent owns deployment: build the modified mock source into an actual new runtime
image/tag, then deploy and verify it before resuming browser acceptance. Do not
retag the prior mock image as the new code. The previously frozen browser bundle
is unchanged. Existing OIDC tests contain hardcoded truncated IDs for freshly
created identities; those assertions need a separately owned update to consume
returned IDs. They are not evidence that the new generation should stay truncated.
