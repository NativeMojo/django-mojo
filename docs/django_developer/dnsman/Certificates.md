# Certificates — ACME DNS-01, custody, renewal, sync

`mojo/apps/dnsman/services/certs.py` + `mojo/helpers/acme/`.

Certificates are issued centrally over **ACME DNS-01** and held here. DNS-01
needs only a TXT record, so it works for every provider we can write a zone
through, needs no webserver, and runs on a worker rather than on a serving box.

## Why not ACM

Free ACM certificates can only be attached to ACM-integrated front-ends (ALB,
CloudFront) — they cannot be installed into nginx on our own instance. The
exportable ACM tier can, but is priced per certificate at issuance **and** at
each renewal on a 198-day validity, which is a recurring per-domain tax that
does not survive contact with a few thousand customer domains.

Let's Encrypt over ACME is $0 and DNS-01 works centrally because we hold, or
hold credentials for, the zones.

## Why an in-repo ACME client

`mojo/helpers/acme/` implements only the DNS-01 slice of RFC 8555, on libraries
already present: `cryptography` for keys and CSRs, `requests` for transport, and
PyJWT's low-level utils for base64url and DER→raw signature conversion.

certbot's `acme` package is more battle-tested, but would add `josepy` and
`pyOpenSSL` to a **published framework** most consumers will never use for
certificates. The client sits behind `services/certs.py` and is swappable if it
ever disappoints.

Protocol details worth knowing, each pinned by a test:

- The flattened JWS is assembled by hand rather than via `PyJWS.encode`, which
  injects a `typ` header field. RFC 8555 servers are strict about the protected
  header — a test asserts `typ` is absent.
- The RFC 7638 thumbprint hashes required members only, lexicographically
  ordered, no whitespace. Get it wrong and the CA computes a different key
  authorization; the only symptom is every validation failing.
- `badNonce` is retried once with the replacement nonce; `Retry-After` is honoured.
- POST-as-GET sends an *empty* payload, not `{}`.
- `revokeCert` signs with `kid` when using the account key.

## The issuance flow

```
new order
  group challenge digests BY RECORD NAME
  upsert each _acme-challenge name once, with its full digest list
  wait for authoritative propagation
  answer each challenge
  poll until ready
  finalize with a fresh P-256 CSR
  download the chain
  store cert + chain + key (KMS), set validity and renew_after
  broadcast "certificate updated"
finally:
  clear every challenge record we planted
```

For a verified tenant `AcmeDelegation`, the challenge-writer branch changes but
the ACME/custody flow does not. The alias is authoritatively re-proved before
`new order`; both apex/wildcard digests are published together through the
challenge-specific hub client at the stored opaque target; propagation probes
that target; and the durable cleanup reference is idempotently withdrawn in
`finally`. Pending delegation is ignored. Verified/broken routing is sticky and
never silently falls back to direct DNS.

### The wildcard trap

`example.com` and `*.example.com` produce **two separate authorizations that
share one record name** — `_acme-challenge.example.com` — and both digests must
be live **at the same time**.

Write them one at a time and the second write erases the first (on GoDaddy
literally, since its PUT replaces the whole record set), so one authorization
always fails. Challenges are therefore grouped by record name and each name is
written once with the complete value list. All upserts happen before any
propagation wait, so both digests are live before either is checked.

### Cleanup is in a `finally`

On success *and* on failure. Otherwise orphaned `_acme-challenge` TXT records
accumulate in customer zones, which is both untidy and confusing to anyone
debugging their own DNS. Cleanup passes the exact digest list, so it touches only
what this issuance planted.

It calls `dns.clear_record`, **not** `dns.delete_record`. GoDaddy has no true
delete and refuses to remove the last value of a record set, so a delete raised —
and cleanup, running in a `finally`, logged and swallowed it, leaving the
challenge TXT live and one digest richer after every renewal. `clear_record` lets
the adapter pick the strongest retirement it has: a real delete on Route53, an
inert placeholder overwrite on GoDaddy. See
[Providers](Providers.md#clear_record--for-callers-with-nowhere-to-put-that-refusal).

A cleanup failure still **never** fails an issuance that otherwise succeeded —
that is the whole point of the `finally` and it is asserted by a test.

### Propagation

`wait_for_txt` (`mojo/helpers/dns/probe.py`) resolves the zone's **authoritative
nameservers and queries them directly** rather than trusting the local recursive
resolver. A recursive answer can be a false negative (cached negative for a
record we just wrote) or, worse, a false positive on a stale value — telling the
CA to validate a record it will never see. Either mistake costs a failed
issuance and burns rate limit.

Route53 additionally gates on `ChangeInfo` reaching `INSYNC` first. GoDaddy has
no equivalent and enforces a 600-second minimum TXT TTL. After its authoritative
probe succeeds for an ACME record, the provider gate therefore waits one full
TTL before asking the CA to validate, preventing secondary validation from
seeing the prior cached challenge value.

Each `dns.*` call resolves a **fresh** provider adapter (see
[Providers](Providers.md#change-ids-and-the-insync-gate)), so the INSYNC gate
only fires when the caller threads the change id back in explicitly. The
issuance loop above captures the `change_id` each `dns.upsert_record` call
returns, keyed by record name, and passes it into the matching
`dns.wait_for_propagation` call. Drop that thread and the gate is silently
skipped — issuance races Route53's anycast propagation instead of waiting on
it, which is exactly how a challenge TXT record could be live in one edge but
still `NXDOMAIN` from Let's Encrypt's vantage. A `wait_for_propagation` call
with no change id at all now logs a warning from `Route53Provider` naming the
gate as skipped.

## Custody

The private key is KMS-envelope-encrypted via `KSMSecrets`. It appears in **no
graph**, in **no job payload**, and leaves the database through exactly one
gated, access-logged endpoint (`GET /api/dnsman/certificate/material/<pk>`),
which requires `manage_dns` rather than `view_dns` — seeing that a certificate
exists is not the same as being entitled to its key.

`KSMSecrets` returns an empty mapping when KMS decryption fails, so an empty key
on an *active* certificate means the custody layer is unavailable, not that the
certificate has no key. The endpoint reports that as `503`, because reporting it
as "no key" would send a consumer off to reissue for no reason.

## Renewal and sync

`renew_certificates` (every 6h) queues a job per certificate past `renew_after`
(`not_after` − `DNSMAN_CERT_RENEW_DAYS`, default 30).

Requests serialize on the Domain row and job publishes carry an internal
idempotency key while retaining the existing `{certificate: pk}` payload.
Execution atomically claims the Certificate before any CA call; a duplicate
worker exits without creating an order. The claim is also protected by a
per-certificate advisory lock held through the remote work. If a worker dies,
the renewal scan requeues the abandoned `issuing` row after
`DNSMAN_CERT_ISSUING_STALE_SECONDS` (default 30 minutes); a live lock prevents a
slow worker from being reclaimed concurrently. Existing still-valid material
remains available from the material endpoint while renewal is `issuing`.
Initial issuance never has material to expose. When a delegated renewal fails and the
existing KMS-held material is still valid, the row returns to `active`, its
cert/chain/key remain untouched, `last_error`/`attempts` record the failure, and
`renew_after` moves to a bounded exponential retry (base
`DNSMAN_CERT_RETRY_BASE_SECONDS`, default one hour; hard maximum 24 hours).

On every successful issue or renewal, dnsman broadcasts a `certificate_updated`
job on `DNSMAN_CERT_SYNC_CHANNEL` carrying **only** the certificate id, domain
and expiry. Subscribed hosts pull the material themselves and reload locally.

After a successful initial issuance, failed attempts for the same domain and
exact SAN set are removed from the live inventory. The Admin certificate page
also exposes `POST certificate/remove-failed` for manual cleanup; it accepts
only `failed` rows and never deletes active material. The Dashboard groups
lifecycle history by domain and SAN set and reports only the newest row, so a
superseded attempt cannot claim that serving TLS is down.

## Retirement

`remove-failed` cleans up a dead attempt; **retirement** (`certs.retire_certificate`)
removes a live one — a certificate an operator no longer needs because another
active certificate on the same domain can fully take over its duty (the common
case: a per-app certificate a domain's later apex-plus-wildcard cert now
covers). It repoints every `Vhost` still referencing the target at the
replacement — `vhost.save()`, never `update()`, so enabled rows re-validate
coverage and publish fleet convergence — then deletes the target row inside
one transaction; `Vhost.certificate` is `PROTECT`, satisfied only once nothing
references it.

`certs.retire_eligibility(domain)` is the DB-only companion the portal calls on
every domain page load: for each `Certificate` on the domain, the id of the
**active**, not-renewal-due certificate that covers every name the candidate
lists (CN + SANs) **and** every enabled vhost still pointed at it, or `None`.
No provider calls, so it is safe on a hot path. `retire_certificate` reruns
that same coverage test authoritatively (via `edge.validators.certificate_covers`
— the exact rule the vhost layer enforces at enable time) and refuses,
naming the reason, when: no other active certificate on the domain covers
every name the target lists; the only candidate that does is itself due for
renewal; or the candidate cannot serve a specific enabled vhost's name. A
retirement can never leave a serving name that nginx would then refuse to
enable.

### `GET /api/dnsman/certificate/retire-eligibility?domain=<pk>`

Returns `{domain, eligibility: {"<certificate id>": <replacement id or null>}}`
for every certificate on that domain. Same guards as the detail route — model
`VIEW_PERMS` first, then the house-domain platform-admin gate for a
group-less domain.

### `POST /api/dnsman/certificate/retire`

`{"certificate": 7}`. Same guards as `remove-failed`/`revoke` — model
`SAVE_PERMS`+`VIEW_PERMS`, then the house-domain guard, since this is
destructive. Returns `{retired, replaced_by, vhosts_repointed}` and logs the
outcome, including the acting user and IP.

dnsman never pushes into a serving box. There is no SSH, no shared filesystem,
and no second auth surface — the job channel already exists.

The payoff is operational: **standing up a replacement host is a sync, not a
reissue.** No new CA order, no rate-limit exposure, and hosts stay disposable.

> `jobs.publish` routes to the channel it is given — the broadcast always goes
> to the configured channel. `certs` (the default) ships in `JOBS_CHANNELS`'
> default list. If you set `JOBS_CHANNELS` explicitly, or override
> `DNSMAN_CERT_SYNC_CHANNEL`, make sure a runner consumes that channel; an
> unconsumed queue raises a `jobs:unconsumed_channel` incident.

## Expiry monitoring

`cronjobs.publish_certificate_expiry` runs hourly and publishes one number for
the whole deployment: the fewest days remaining across every certificate, as the
CloudWatch metric `DjangoMojo/Certificates` / `MinDaysToExpiry`, dimensioned by
deployment slug. `aws-check`'s `monitoring` section creates the alarm that reads
it, with `TreatMissingData=breaching` — so this job going quiet is itself the
alarm. One signal catches every cause of a stalled renewal at once: publisher
down, challenge misrouted, credentials wrong, delegation record deleted.

Three behaviours worth knowing:

- **`failed` certificates still count.** `_record_issue_failure` moves a
  certificate to `failed` once its material is no longer valid, so filtering on
  `active` alone would drop a certificate at the moment it expires — the
  published minimum would jump to the next-soonest and CloudWatch would report
  the alarm *recovered* while TLS is actually broken. `revoked` rows and rows
  with no `not_after` are excluded.
- **Expired certificates publish a negative number**, not a clamped zero. `-3` is
  more diagnostic than `0`, and the alarm catches both. The consequence is that a
  permanently-dead certificate row pins the deployment-wide minimum and the alarm
  never clears — **revoke or delete the row**; do not retune the threshold.
- **No certificates publishes nothing at all.** A deployment with none should
  read as un-set-up (the alarm sits in INSUFFICIENT_DATA), not as healthy.

The job needs `cloudwatch:PutMetricData` on that namespace, granted to the
identity the **job runners** use — not to the operator running `aws-check`.
Without credentials the job **fails loudly** (`NoCredentialsError` out of
`put_metric_data`) and shows up in the jobs surface; it is not a silent no-op.
That is deliberate — a publisher that quietly did nothing would leave the expiry
alarm looking merely un-set-up.

## Staging is the default

`DNSMAN_ACME_DIRECTORY_URL` defaults to the Let's Encrypt **staging** directory.
An unconfigured deployment that starts issuing therefore cannot consume
production rate limits; going live is a deliberate change.

Let's Encrypt caps new orders per account per window, so bulk issuance should
run through the job queue with backoff rather than a burst loop.

## Fallback

HTTP-01 on the serving box remains the documented fallback for zones whose DNS
we cannot write at all. It is not implemented here — DNS-01 covers every domain
dnsman manages, by definition.

## Durable DNS reservations

Direct-provider DNS-01 issuance persists the complete TXT value set before the
first write and commits `mutation_attempted` before provider I/O. Wildcard/apex
values therefore share one reservation and one provider mutation. Reservation
and interactive complete-set writes serialize on the stable parent domain even
when no reservation row exists yet. A timeout is reconciled by exact
authoritative inventory; absent or mismatched inventory leaves the attempted
intent durable and raises the original provider failure rather than blindly
replaying it. Cleanup releases ownership only after the provider accepted the
removal. A failure leaves `cleanup_pending`, and the next issuance must repair
that old intent before creating a new CA order.

A reconciled upsert returns `change_id="reconciled"` (`dns.RECONCILED_CHANGE_ID`)
rather than a pollable Route53 change batch id, since the write converged
through inventory comparison and not a fresh API call. `wait_for_propagation`
drops that sentinel before calling the adapter — see
[Providers](Providers.md#change-ids-and-the-insync-gate) — so propagation still
runs, just without the INSYNC gate for that record.
