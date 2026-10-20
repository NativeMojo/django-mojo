# ACME delegation federation hub

The optional ACME hub lets a downstream deployment complete DNS-01 without
receiving credentials for the hub AWS account. It is dormant until
`DNSMAN_ACME_HUB_ZONE` is set and is independent of `Domain.provider`, direct
Route53/GoDaddy certificate issuance, and Maestro Sites HTTP-01.

## Trust and DNS shape

An operator provisions an `ApiKey` on the downstream project's `Group` with
`dnsman_acme_federation`. That permission is in django-mojo's protected API-key
floor. An ordinary group administrator cannot assign it: the normal member path
requires the global `sys.dnsman_acme_federation` grant, while the existing
global `manage_users` / `manage_groups` key-administration override can also
assign it. The three legacy allocation/publish/withdraw endpoints reject JWT users, group tokens, anonymous
callers, keys without the underlying permission, override sessions whose
underlying key lacks it, and keys whose project or ancestor project is inactive.
Authorization and ownership always come from `request.api_key.group`; a
caller-supplied group and an override user's organization are never identities
for this surface.

The downstream onboards a domain with a fresh immutable `client_ref`. The hub
returns a permanent allocation:

```text
source: _acme-challenge.customer.example
target: 4a0f…128-bit-random….acme-hub.example.net
```

The tenant publishes that one CNAME. Before every challenge publish, the hub
proves the source is exactly one CNAME hop to the stored target; a chained or
mismatched CNAME is refused. Withdrawal does not repeat the proof, so revoking
the CNAME cannot prevent cleanup.

## Persistence and reconciliation

`AcmeHubDelegation` is a permanent tombstone keyed by project UUID plus
`client_ref`. The project UUID, normalized domain/source, random target,
original group attribution, and exact allocation zone name/id never change.
Changing configuration affects only new allocations and a target is never
reused, including re-onboarding the same domain with a new reference.

`AcmeHubChallengeLease` durably stores one immutable participant-local `challenge_ref`, its exact
complete digest set, state, expiry, and reconciliation timestamps. Publish
commits pending intent before touching Route53. A PostgreSQL advisory lock per
allocation covers the lease write and the provider write, and Route53 always
receives the union of every pending/active lease. This preserves the
apex-plus-wildcard case where two ACME authorizations share one record.

**Publish and withdraw return once the lease is durable and Route53 has
ACCEPTED the change — they do not wait for it to propagate.** The lock no
longer spans a propagation wait. This is not an optimization: issuance never
runs on an HTTP request (`certs.issue` is only ever called from a job), so the
downstream's own propagation wait already runs on a job worker with a full
budget, while its read timeout on this call is 30 seconds. A hub-side wait
bounded at 300 seconds could never be satisfied before the caller gave up, so
every delegated issuance failed. The caller owns the propagation gate; see
[Certificates](Certificates.md#propagation).

`reconciled_at` therefore means one specific thing: **a probe confirmed that
the authority serves this allocation's exact RRset.** It is stamped by the
sweep on a confirmed match and by nothing else — never merely because a write
was submitted.

Read it as an internal reconciliation signal, not as a propagation guarantee.
The sweep's probe is the ordinary first-responder `query_txt`, so it confirms
that *an* authoritative nameserver serves the RRset, not that the authority set
agrees. Nothing gates issuance on `reconciled_at`; the gate that does is the
caller's majority quorum, described above.

The five-minute `dnsman.cronjobs.sweep_acme_hub_leases` dispatcher queues the
worker sweep. Every publish and every withdrawal now leaves its allocation
unconfirmed, so the sweep is the normal path rather than a rare repair: it
expires stale leases, probes each candidate allocation, and writes only on a
real mismatch. An unreadable authority is neither a match nor a mismatch — the
allocation is left for the next pass, because rewriting on an unreadable probe
would turn a resolver outage into a Route53 write storm. Failed passes file a
suppressed `dnsman:acme_hub:sweep` incident. Provider failures remain loud;
there is no fallback to another challenge or provider.

## Zone safety and observability

Allocation and every write revalidate the stored Route53 zone as an exact,
public hosted zone whose public NS cut matches Route53's delegation set. The
writer is internal to `services/acme_hub.py`; it is not reachable through the
tenant DNS record endpoints.

Audit rows cover allocation, publish/refusal, withdrawal, expiry and provider
failure with project, API-key/request attribution and counts. TXT digests,
hosted-zone ids, AWS change ids and credentials are never logged or returned.

## Hub HTTP contract

All three routes are `POST` requests under `/api/dnsman`, require
`Authorization: apikey <token>`, and return HTTP 200 with the normal mojo
envelope on success. `client_ref` and `challenge_ref` are 1–128 characters,
start with a letter or digit, and otherwise accept letters, digits, `.`, `_`,
`:`, and `-`. A publish accepts 1–20 entries (duplicates are deduplicated),
each 1–1024 characters; NUL, CR, and LF are rejected.

`POST /api/dnsman/acme/delegation`:

```json
{ "domain": "customer.example", "client_ref": "site-install-2026-08" }
```

```json
{
  "status": true,
  "code": 200,
  "data": {
    "client_ref": "site-install-2026-08",
    "domain": "customer.example",
    "source": "_acme-challenge.customer.example",
    "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net"
  }
}
```

The same project/ref/domain replay returns the same target. The same ref with a
different domain is a 400 immutable-reference conflict.

`POST /api/dnsman/acme/challenge/publish` accepts:

```json
{
  "client_ref": "site-install-2026-08",
  "challenge_ref": "order-123",
  "values": ["digest-for-apex", "digest-for-wildcard"]
}
```

It adds `challenge_ref` and `active_value_count` to the allocation payload:

```json
{
  "status": true,
  "code": 200,
  "data": {
    "client_ref": "site-install-2026-08",
    "domain": "customer.example",
    "source": "_acme-challenge.customer.example",
    "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net",
    "challenge_ref": "order-123",
    "active_value_count": 2
  }
}
```

A replay must carry the same values. A withdrawn or expired reference cannot
be republished. `active_value_count` is the size of the deduplicated union
currently required by all live leases, not the number of leases.

`POST /api/dnsman/acme/challenge/withdraw` accepts only the two references and
returns the same challenge response shape with the remaining union count:

```json
{ "client_ref": "site-install-2026-08", "challenge_ref": "order-123" }
```

Unknown and already-retired challenge references are idempotent successes.
All three routes share a sliding rate-limit bucket: 120 requests/minute per IP
and 300 requests/minute per project (the limiter's API-key dimension is grouped
by the key's group), returning 429 with `Retry-After` when engaged.

| HTTP | Meaning |
|---:|---|
| `400` | Missing/invalid input, unknown allocation, immutable-ref conflict, retired challenge ref, or missing/mismatched/chained CNAME proof |
| `403` | Not an ApiKey, missing protected permission, inactive key, or effectively inactive project |
| `429` | Shared hub rate limit exceeded |
| `503` | Hub disabled/misconfigured, exact public zone unavailable, or Route53 failure |

Errors use the normal `{"status": false, "code": <HTTP>, "error": "..."}`
envelope. They never echo submitted TXT values or infrastructure identifiers.

## File-only settings

| Key | Default | Bounds / meaning |
|---|---:|---|
| `DNSMAN_ACME_HUB_ZONE` | unset | Exact public Route53 zone; unset disables the hub |
| `DNSMAN_ACME_HUB_HOSTED_ZONE_ID` | unset | Optional exact zone id (recommended where duplicate names exist) |
| `DNSMAN_ACME_HUB_TTL` | `60` | TXT TTL, clamped to 30–86400 seconds |
| `DNSMAN_ACME_HUB_LEASE_SECONDS` | `900` | Lease lifetime, clamped to 60–86400 seconds |
| `DNSMAN_ACME_HUB_SWEEP_LIMIT` | `100` | Allocations per sweep, clamped to 1–1000 |

`DNSMAN_ACME_HUB_PROPAGATION_TIMEOUT` and `DNSMAN_ACME_HUB_PROPAGATION_INTERVAL`
are **retired** — the hub no longer waits for propagation. They are ignored if
still present in a config file.

These settings intentionally use `settings.get_static`: no tenant or DB-backed
setting can redirect an existing or future allocation.

## Legacy publish-propagation version skew

This section describes the historical legacy publish-propagation fix. Shared
validation v2 separately detects capabilities404 as unsupported and requires the
[coordinated hub rollout](#coordinated-hub-rollout) below.

The propagation fix belongs on the **hub** side, and only there.

* **Old client → new hub**: works. This is exactly the deployment that is
  broken today, and upgrading the hub alone repairs it — the reply shape is
  byte-for-byte unchanged, so no downstream change is required to benefit.
* **New client → old hub**: unchanged and still broken. The old hub still
  blocks its reply for up to 300 seconds behind a 30-second read timeout; the
  client's stronger quorum gate never gets to run because the publish call
  itself times out first.

So a mixed fleet must upgrade hubs first. A downstream running the newer
majority-quorum gate against an upgraded hub is the intended end state.

## Downstream challenge client

A deployment consuming this hub uses
`mojo.apps.dnsman.services.acme_hub_client`. This is a challenge-specific HTTP
transport, not a `DnsProvider`: it is never registered with
`services/dns.get_adapter()`, cannot list records, and cannot write a
caller-selected name. Its legacy challenge operations are:

```python
allocation = acme_hub_client.allocate(domain, client_ref)
published = acme_hub_client.publish(client_ref, challenge_ref, record_values)
withdrawn = acme_hub_client.withdraw(client_ref, challenge_ref)
```

Persist the returned normalized `domain`, exact `_acme-challenge` `source`, and
opaque `target` with the immutable `client_ref`; the tenant publishes that one
CNAME. Persist cleanup intent and the locally generated immutable
`challenge_ref` before calling `publish()`. Always call the idempotent
`withdraw()` for that reference after success, failure, or an ambiguous publish
response. Delegated propagation probes the persisted `target`, not the tenant
`source`.

The downstream settings are also file-only and read at call time:

| Key | Default | Bounds / meaning |
|---|---:|---|
| `DNSMAN_ACME_HUB_URL` | unset | Hub HTTPS origin; plain HTTP is accepted only for `localhost`/loopback development |
| `DNSMAN_ACME_HUB_API_KEY` | unset | Project ApiKey carrying protected `dnsman_acme_federation` |
| `DNSMAN_ACME_HUB_CONNECT_TIMEOUT` | `5` | Connect timeout, 0.1–30 seconds |
| `DNSMAN_ACME_HUB_READ_TIMEOUT` | `30` | Read timeout, 0.1–120 seconds |
| `DNSMAN_ACME_HUB_RETRIES` | `1` | Identical idempotent retries, 0 or 1 |

There is deliberately no downstream zone-name setting: allocation returns the
full target. `is_available()` returns false only when both required settings
are absent; partial or unsafe configuration raises
`AcmeHubConfigurationError`. Calls never follow redirects, use normal TLS
verification, bound response bodies, validate echoed references and DNS names,
and map remote bodies to typed, bounded errors safe for certificate/job status.
Only connect/read ambiguity and HTTP 502/503/504 are retried, at most once;
400/401/403/409/429 and redirects are not. Missing configuration or any client
failure is loud and never falls back to Route53, GoDaddy, or another challenge.

Client failures expose only bounded metadata: `kind`, `retriable`, and (for
HTTP responses) `http_status`. Configuration/request validation is not
retriable; transport ambiguity is retriable; only 502/503/504 HTTP responses
are marked retriable. Remote response bodies, configured URLs/API keys, and TXT
values are never copied into exception text.

## Operator entry point

The REST contract above is the programmatic surface. For bootstrapping a domain
by hand there is a CLI wrapper over the same services:

```bash
python manage.py aws-check --apply --section dns --dns-domain customer.example --dns-group 7
```

It allocates the delegation, prints the `_acme-challenge` CNAME to hand to the
domain owner, **verifies that CNAME resolves before requesting anything**, and
then queues issuance. The verification step is load-bearing: Let's Encrypt caps
failed validations at 5 per account per hostname per hour, so issuing against an
unpropagated record blocks retries for an hour during the exact activity —
bootstrapping — where someone is iterating. See
[../aws/aws_check.md](../aws/aws_check.md#the-dns-section).

## Downstream tenant lifecycle

`AcmeDelegation` is the consuming deployment's durable tenant-side tombstone.
It commits a UUID `client_ref`, tenant/user snapshots and normalized name before
the first `allocate()` call, then stores the returned source/target once and
compares every later hub reply to them. Pending external names do not create or
reserve a globally unique `Domain`; authoritative exact one-hop proof creates
`Domain(provider="mojo")` atomically. That provider is certificate-only and is
never registered with general DNS CRUD.

The public lifecycle is `pending → verified ↔ broken`; `retired` tombstones are
internal. Legacy `allocated` pending rows are inert; prepared `shared` rows are
route guards immediately. Once verified, routing is sticky: a changed,
missing, chained or conflicting alias marks the delegation broken and issuance
fails closed without falling back to the Domain's old Route53/GoDaddy adapter.
Verification locks and rechecks the original tenant identity and its effective
active state before creating a Domain, so a deleted/deactivated tenant cannot
turn an external name into a house asset.

Delegated v1 accepts exactly apex plus wildcard. Issuance re-proves the alias
before creating a CA order, publishes both digests in one hub lease, probes the
opaque target for the exact values, and records cleanup intent before publish.
The same immutable reference is withdrawn in `finally`, including after an
ambiguous timeout. Certificate workers claim a row atomically and Jobs uses an
idempotency key, so duplicate requests/jobs consume one order. A failed renewal
keeps still-valid KMS-held material active and advances `renew_after` with a
bounded retry instead of permanently failing the serving certificate.

The tenant REST payload is deliberately narrower than either tombstone model:

```json
{
  "id": 9,
  "created": "2026-08-06T12:00:00+00:00",
  "modified": "2026-08-06T12:03:00+00:00",
  "domain": 12,
  "domain_name": "customer.example",
  "source": "_acme-challenge.customer.example",
  "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net",
  "state": "verified",
  "verified_at": "2026-08-06T12:03:00+00:00",
  "last_error_code": null
}
```

For an external pending name, `domain` and `verified_at` are null. Initiate and
verify require `manage_dns` (or `security`); detail/list require `view_dns`,
`manage_dns`, or `security`. A missing/retired detail is 404, cross-tenant
access is 403, invalid request combinations and failed proof/name claims are
400, and missing/invalid downstream hub configuration or allocation transport
failure is 503. `last_error_code` is a bounded machine diagnostic such as
`alias_lookup_failed`, `alias_mismatch`, `domain_claimed`, `group_inactive`,
`configuration`, `transport`, `response`, `http`, or
`hub_allocation_mismatch`; UI copy should be driven by the HTTP error and state,
not by displaying that internal code verbatim.


## Shared validation (protocol v2)

An existing allocation controller can authorize explicitly named projects to use
that allocation's **existing** public target. No new CNAME or target is created.
This grants certificate issuance authority for the apex **and** wildcard. A
trusted deployment can obtain a certificate using its own private key; granting
trust can therefore enable impersonation. No private key, ACME account,
registrar credential or general DNS capability crosses this protocol.

A controller is the allocation's original project group through a fresh active
protected federation key, or a genuine interactive platform superuser using the
shared `services.authorization.is_platform_admin` policy. Acting-superuser
keys, GroupScopedToken, ordinary DNS/Sites/security managers, key administrators
and a caller-selected group cannot confer this authority. Every management
operation repeats the allocation-row check. Protected-key capabilities indicate
eligibility, never authority over every allocation.

`AcmeHubParticipant` binds a permanent random `participant_ref`, immutable
consumer project UUID, original group, consumer-local `client_ref`, allocation
and role (`owner` or `trusted`). `(consumer_project_uuid, client_ref)` is unique
across both roles. One owner is backfilled for every old allocation, including
allocations whose group was deleted; historical leases are bound to that owner
without changing PKs, values, state, expiry, errors or reconciliation timestamps.
A null original group cannot be inherited by a recreated project.

Grant creates a trusted participant; join only validates that grant and exact
one-hop alias, and creates nothing. Revocation is a permanent tombstone. Exact
active grant retries are idempotent; revoked references never reactivate or
rebind. Regrant needs a fresh persisted consumer-local ref. Owner participants
cannot be revoked through trust APIs. Legacy allocate creates allocation and
owner participant atomically and conflicts on an already-trusted local ref,
rather than minting a competing target.

Every lease has a required participant FK and uniqueness on
`(participant, challenge_ref)`. All writes, withdrawals, revocations, expiry and
reconciliation hold the existing allocation advisory lock through provider
submission. Publication repeats fresh key/group and trust checks **inside** the
lock after DNS proof. The union excludes elapsed and revoked participants, and
deduplicates identical values without conflating challenge refs across projects.
Revocation commits before provider cleanup. Ambiguous provider failures leave
durable, unconfirmed cleanup for repeated revoke/withdraw or the fair sweep.
An active rotated protected key in the same original group may withdraw an old
revoked participant's leases without alias proof; dead credentials and inactive
projects are never admitted for cleanup.

Cleanup is bounded by the existing lease lifetime
`DNSMAN_ACME_HUB_LEASE_SECONDS` (default 900, clamped 60–86400 seconds), the
five-minute sweep cadence and provider availability. Do not promise cleanup
while the provider is unavailable. Revocation cannot revoke an already-issued
certificate or undo a CA request accepted before revocation.

### Connection request handshake

Read-only preflight calls `acme_hub_client.capabilities()` and
`lookup(domain, allocation_ref=None, cursor=None)` before any local row is
created. A hub returning capabilities404 reports
`shared_validation_unsupported`; never fall back to a new allocation for an
already-used CNAME. Existing ordinary DNS and legacy delegation remain usable.

Project setup owns preparing a fresh persisted local ref and exporting:

```json
{"version":1,"consumer_project_uuid":"0123456789abcdef0123456789abcdef",
 "client_ref":"persisted-consumer-local-ref","domain":"customer.example",
 "allocation_ref":"existing-target.acme-hub.example.net"}
```

The object contains no URL/key/token and has no authority by itself. The
controller resolves the exact recipient, recognizes its bounded label and the
apex+wildcard scope, and explicitly grants the descriptor. The consumer rechecks
and joins with the **same persisted ref**. Public CNAME knowledge cannot join or
self-grant. A non-controller can export a request and await authorization without
creating a privileged credential or editing database rows.

The narrow configured client adds `status(client_ref)`,
`join(domain, client_ref, allocation_ref)`, `list_allocations(domain, cursor=None)`,
`list_participants(allocation_ref, cursor=None)`,
`resolve_recipient(allocation_ref, consumer_project_uuid)`, `grant(connection_request)`
and `revoke(allocation_ref, participant_ref)`. Helpers use fixed paths and strict
separate v2 DTO validation; no caller URL/key arguments. Existing file-only
`DNSMAN_ACME_HUB_URL` and `DNSMAN_ACME_HUB_API_KEY` settings retain custody, TLS,
redirect, retry and timeout safeguards. Shared responses have a bounded 64KiB
wire budget to accommodate 20 complete DTOs; legacy replies retain 16KiB.

The consumer/controller HTTP contract, exact DTO keys, safe error tokens and
pagination are documented in the [REST reference](../../web_developer/dnsman/README.md#shared-validation-v2).
Alias status is an authoritative single-source CNAME observation, **not** TXT
propagation or certificate convergence. Consumer responses never disclose other
participants' identities, lease refs or TXT values. Audit entries record action,
result, actor, participant UUID, consumer UUID, role and counts, never digests,
zone IDs or secrets.

### Coordinated hub rollout

Drain **all** old hub HTTP writers and sweep workers, migrate/backfill through
the nonnull participant FK, then resume only new hub binaries before enabling
shared use. Old hub binaries use allocation/challenge lookups that can touch
another participant's lease; they cannot coexist safely with this schema.
Old downstream clients remain wire-compatible with the upgraded hub: the
allocation data stays exactly four fields and publish/withdraw exactly six,
echoing each participant's own local ref. Configuration DTOs are unchanged.
The migration is intentionally irreversible; do not downgrade hub binaries after
trusted rows exist. Direct-provider Domain rows and certificates stay in place.
Portal/Sites adoption, package release and production deployment remain
separate work; this protocol and its project setup flow do not lift any
production hold.

## Supported project setup

`services/delegation_setup.py` is the local contract for an authorized project
operator or agent to reuse an existing allocation. REST, `aws-check` and the
[DNS assistant tools](../assistant/dns_tools.md) return the same versioned
[Readiness v1](../../web_developer/dnsman/README.md#readiness-v1) object.
Inspection, durable preparation, joining, persisted proof and certificate
request are separate actions. None of them grants hub trust.

The target is either an existing local `Domain` id, or an active local group
plus a normalized apex name. An existing same-name Domain must belong to that
exact group. Setup never adopts a house zone, moves a Domain, creates a group,
changes its provider/credential/hosted-zone configuration, or replaces its
certificate or vhost references. A groupless house Domain is read-only and
ineligible: mutation capabilities are false, no descriptor is exported, and
`next_action` is `unsupported_domain_scope` for the deployment operator.
Shared setup currently requires a group-managed domain; existing direct-provider
operation remains available.

The local tenant UUID and hub consumer project UUID are different identities.
The consumer UUID comes only from capabilities authenticated by the configured
hub key's original group. It must never be inferred from `Domain.group`, an
interactive controller session, DNS, or user input.

### Prepare, authorize, connect, check, request

The examples use fake public names and references. Authentication is omitted.
A project administrator performs these local calls with the appropriate DNS
permission; only the allocation controller performs the intervening grant.

1. Inspect without creating rows, refs, accounts or jobs:
   `GET /api/dnsman/delegation/plan?domain=12&allocation_ref=existing-target.acme-hub.example.net`.
   Failed DNS observations do not demote a verified connection on this read.
2. Prepare with `POST /api/dnsman/delegation/connect` and
   `{"domain":12,"allocation_ref":"existing-target.acme-hub.example.net"}`.
   Local authorization and read-only hub capabilities/own-participant lookup
   precede preparation. Unconfigured, unsupported or unauthorized preflight
   leaves direct routing unchanged. Successful preparation commits a fresh
   immutable ref, shared mode, allocation and consumer identity before an
   ambiguous status/join call. Missing trust yields `grant_trust` and a public
   request of exactly this shape:

   ```json
   {
     "version": 1,
     "consumer_project_uuid": "0123456789abcdef0123456789abcdef",
     "client_ref": "11111111-2222-4333-8444-555555555555",
     "domain": "customer.example",
     "allocation_ref": "existing-target.acme-hub.example.net"
   }
   ```

   Export the object as returned. It contains no secret and has no authority
   by itself. Display the scope separately as
   `["customer.example","*.customer.example"]`.
3. The controller imports that object into
   `POST /api/dnsman/acme/trust/grant` as `{"connection_request": ...}` after
   recognizing the exact recipient and the issuance authority being granted.
   Ordinary local `manage_dns` cannot perform this grant or mint the protected
   key. The existing public CNAME and allocation remain unchanged.
4. Replay the same local Connect input after grant. It reuses the stored ref,
   rechecks the configured consumer identity and pins the exact participant,
   source and target. A lost response is retried with that same ref. Changing
   the configured key to a different project reports `identity_changed` and
   cannot repin the connection.
5. Run `POST /api/dnsman/delegation/check` with `{"delegation":9}`. This is
   the explicit state-changing proof step: it rechecks local ownership, active
   grant, fixed scope and exact one-hop CNAME, then reuses `delegation.verify`.
   An existing direct-provider Domain is bound without rewriting it. A new
   external name becomes the existing certificate-only `provider="mojo"`
   Domain only after proof succeeds.
6. Run `POST /api/dnsman/delegation/request` with `{"delegation":9}`.
   The exact apex-plus-wildcard request is queued through the existing
   certificate service. An exact valid certificate outside its renewal
   window returns `already_current`; pending/issuing returns `already_queued`;
   a due active certificate renews its existing id. Conflicting SAN profiles
   are refused rather than converted. Poll Plan for status.

### Immutable generations and routing

`AcmeDelegation.connection_mode` defaults old rows to `allocated`; `shared`
rows pin `hub_allocation_ref`, `hub_consumer_project_uuid` and, once known,
`hub_participant_ref`. These are server-owned immutable bindings. Generic
creation/update/deletion remain disabled and `DENY_AI` blocks model-tool
bypass. Client refs stay unique and the active tenant/name constraint remains.
The target is no longer globally unique across local historical generations:
a successor must be able to reuse the existing public target.

Connect with `restart:true` is an explicit recovery action only for a revoked
binding or an unjoined pending request. It retires the old generation and
commits a fresh ref for the same allocation; the old ref is never edited,
revived or given the new grant. Outstanding cleanup must be settled first.
Export the new descriptor and ask the controller for a new grant. Repeating
ordinary Connect keeps the existing ref. Neither path resets valid certificate
material, `renew_after`, certificate ids or vhost references.

`delegation.routing_for_domain(domain)` is the canonical route selector;
`for_domain` is its compatibility wrapper. It chooses the newest live shared
request, including pending, or an ever-verified tombstone, including retired.
`routing_for_domains(domain_ids)` returns a domain-id-to-row map in one query;
`routed_domain_ids()` returns a composable one-column queryset with the same
selector. Consumers must use these helpers instead of reproducing the rule or
checking only `Domain.provider`. A returned row is a route/deny guard, not proof
that a usable wildcard private key exists. Sites must separately establish
active, readable, exact-profile DNSMan material before selecting it.

Preparation locks Domain first, collects certificate ids in stable order, and
tries each existing certificate issue advisory lock before taking Certificate
row locks. A busy issuer is refused immediately. This avoids the inverse lock
order of an issuer holding the advisory lock before its joined
Certificate/Domain row lock. Direct reservations and delegated cleanup are
settled before the shared guard commits; locks are released before join calls.
No worker performs direct-provider reservation cleanup after it has selected
shared routing. Shared issuance refreshes grant and consumer identity before
CA account/order creation and again before publish. Withdrawal continues to
use the original local ref/challenge ref after revocation; it does not require
active publish trust. Inactive keys/groups remain refused and hub expiry/sweep
owns eventual cleanup when credentials are unavailable.

### Deploying and recovering the downstream

Deliver `DNSMAN_ACME_HUB_URL` and `DNSMAN_ACME_HUB_API_KEY` through the
installation's existing static Django settings delivery. They are read with
`settings.get_static`; the normal server loader reads `VAR_ROOT/django.conf`
once per process. Restart API processes and all issuance workers after
configuration or binary updates. No database setting, browser credential
field, CLI key option, remote-agent key input or credential readback is added.
The configured normalized origin and fixed paths remain the only destinations.

Upgrade the hub first using its drain/migrate sequence above. Apply the next
DNSMan migration, which defaults legacy rows to allocated mode, adds nullable
shared bindings and relaxes historical target uniqueness. It does not rewrite
Domain or Certificate rows. Drain every old downstream issuance worker and
restart API/worker processes with the new routing/grant checks before accepting
shared Connect. Old workers ignore pending shared guards and cannot safely
handle these rows. Prove scheduled renewal with stubbed hub/DNS/CA and the
existing KMS/edge handoff before any separately authorized production adoption.
`renewal.worker_version` remains unknown without established worker telemetry;
the API version cannot prove a long-lived worker restarted.

Before any shared connection, application code can be rolled back with the
additive schema retained. Once shared rows exist, keep the new guards: do not
downgrade binaries, delete rows, or retire a connection merely to regain direct
renewal. Retain still-valid serving material, revoke trust through the core if
needed, repair static configuration/alias/grant, and use explicit fresh-ref
restart where allowed. Returning a formerly shared name to direct DNS requires
a separately authorized DNS handover. The software change authorizes no live
DNS, credential, certificate, vhost or production-adoption change.
