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
no equivalent, so the probe is the only signal there.

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

On every successful issue or renewal, dnsman broadcasts a `certificate_updated`
job on `DNSMAN_CERT_SYNC_CHANNEL` carrying **only** the certificate id, domain
and expiry. Subscribed hosts pull the material themselves and reload locally.

dnsman never pushes into a serving box. There is no SSH, no shared filesystem,
and no second auth surface — the job channel already exists.

The payoff is operational: **standing up a replacement host is a sync, not a
reissue.** No new CA order, no rate-limit exposure, and hosts stay disposable.

> `jobs.publish` silently falls back to the `default` channel when the
> configured channel is absent from `JOBS_CHANNELS`. Add `certs` to the
> consuming runner's channel list or the broadcast lands where nobody listens.

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
