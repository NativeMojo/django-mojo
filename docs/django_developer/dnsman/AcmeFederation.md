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
assign it. The three endpoints reject JWT users, group tokens, anonymous
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

`AcmeHubChallengeLease` durably stores one immutable `challenge_ref`, its exact
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

## Version skew

The fix belongs on the **hub** side, and only there.

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
caller-selected name. The only operations are:

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
internal. Pending is inert. Once verified, routing is sticky: a changed,
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
