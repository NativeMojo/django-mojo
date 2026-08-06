# ACME delegation federation hub

The optional ACME hub lets a downstream deployment complete DNS-01 without
receiving credentials for the hub AWS account. It is dormant until
`DNSMAN_ACME_HUB_ZONE` is set and is independent of `Domain.provider`, direct
Route53/GoDaddy certificate issuance, and Maestro Sites HTTP-01.

## Trust and DNS shape

An operator provisions an `ApiKey` on the downstream project's `Group` with
`dnsman_acme_federation`. That permission is in django-mojo's protected API-key
floor and therefore requires the global `sys.dnsman_acme_federation` grant to
assign. The three endpoints reject JWT users, group tokens, anonymous callers,
keys without the underlying permission, override sessions whose underlying key
lacks it, and keys whose project or ancestor project is inactive.

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
allocation covers the whole reconciliation, including the provider write, and
Route53 always receives the union of every pending/active lease. This preserves
the apex-plus-wildcard case where two ACME authorizations share one record.

The five-minute `dnsman.cronjobs.sweep_acme_hub_leases` dispatcher queues the
worker sweep. It expires stale leases and retries any unreconciled intent after
worker death or an ambiguous provider response. Provider failures remain loud;
there is no fallback to another challenge or provider.

## Zone safety and observability

Allocation and every write revalidate the stored Route53 zone as an exact,
public hosted zone whose public NS cut matches Route53's delegation set. The
writer is internal to `services/acme_hub.py`; it is not reachable through the
tenant DNS record endpoints.

Audit rows cover allocation, publish/refusal, withdrawal, expiry and provider
failure with project, API-key/request attribution and counts. TXT digests,
hosted-zone ids, AWS change ids and credentials are never logged or returned.

## File-only settings

| Key | Default | Bounds / meaning |
|---|---:|---|
| `DNSMAN_ACME_HUB_ZONE` | unset | Exact public Route53 zone; unset disables the hub |
| `DNSMAN_ACME_HUB_HOSTED_ZONE_ID` | unset | Optional exact zone id (recommended where duplicate names exist) |
| `DNSMAN_ACME_HUB_TTL` | `60` | TXT TTL, clamped to 30–86400 seconds |
| `DNSMAN_ACME_HUB_LEASE_SECONDS` | `900` | Lease lifetime, clamped to 60–86400 seconds |
| `DNSMAN_ACME_HUB_PROPAGATION_TIMEOUT` | `300` | Reconciliation timeout, clamped to 5–900 seconds |
| `DNSMAN_ACME_HUB_PROPAGATION_INTERVAL` | `5` | Poll interval, clamped to 1–30 seconds |
| `DNSMAN_ACME_HUB_SWEEP_LIMIT` | `100` | Allocations per sweep, clamped to 1–1000 |

These settings intentionally use `settings.get_static`: no tenant or DB-backed
setting can redirect an existing or future allocation.

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
