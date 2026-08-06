# dnsman — domains, DNS, email records and TLS certificates

`mojo.apps.dnsman` is the **mechanism** layer for domain-name management:
buying domains, managing DNS records across more than one provider, WHOIS and
privacy, applying SES email records, and centrally issuing and holding TLS
certificates.

It deliberately carries **no product policy**. Spend caps, allowlists, credits,
quote/confirm UX and per-tenant purchase rules belong to the application built
on top. What lives here is the part every product would otherwise rebuild.

REST reference for API consumers: [web_developer/dnsman](../../web_developer/dnsman/README.md).

## Layout

```
mojo/helpers/aws/route53.py      Route53 Domains + hosted-zone primitives (model-free)
                                 incl. list_registered_domains() / list_hosted_zones()
                                 — paginated account inventory, report `truncated`
mojo/helpers/acme/               Minimal ACMEv2 DNS-01 client (jws.py, client.py)
mojo/helpers/dns/godaddy.py      GoDaddy record API (pre-existing; gained raise_on_error)
mojo/helpers/dns/probe.py        Authoritative TXT probe + exact one-hop CNAME proof

mojo/apps/dnsman/
  models/        DnsCredential, Domain, DomainPurchase, AcmeAccount, Certificate,
                 AcmeHubDelegation, AcmeHubChallengeLease
  services/
    naming.py    Domain/record normalization — the single source of truth
    dns.py       Provider dispatch (UNGATED mechanism)
    providers/   route53_provider.py, godaddy_provider.py behind one interface
    onboarding.py  link a credential, adopt a zone, claim a BYO domain,
                   discover the house AWS account's inventory
    registrar.py   search / quote / purchase / poll / WHOIS / privacy
    certs.py       ACME DNS-01 issuance, renewal, revocation, sync broadcast
    acme_hub.py    optional delegated DNS-01 target allocation + lease reconciliation
    email.py       provider-dispatched SES record application
  rest/          thin handlers — this is where permissions are enforced
  cronjobs.py    thin dispatchers — poll registrations / ACME leases (5m), renew certs (6h)
  asyncjobs.py   issue/renew certificate job handlers
```

The helpers are model-free and Django-free on purpose: `ses_domain.py` can call
`route53.py` without importing this app, and both are unit-testable with no
database.

## Provider dispatch

`Domain.provider` is `route53` or `godaddy`. `services/dns.py` resolves a domain
to an adapter and calls one uniform interface:

```python
list_records(domain)                                          # -> [objict(type, name, record_values, ttl)]
upsert_record(domain, rtype, name, record_values, ttl=300)
delete_record(domain, rtype, name, record_values=None)
wait_for_propagation(domain, rtype, name, record_values, timeout=None, change_id=None)
```

> **`record_values`, never `values`.** `objict` subclasses `dict`, so an
> attribute named `values` resolves to the bound `dict.values` method and
> returns a method object instead of your data. This bit two independent
> implementations during the original build. A test pins the field name.

Everything provider-specific is confined to the adapter, because the two
back-ends differ in ways that fail *silently*:

| | Route53 | GoDaddy |
|---|---|---|
| Record name | FQDN | relative label, `@` at the apex |
| TXT values | must be quoted, chunked at 255 chars | stored raw |
| Write semantics | true upsert | PUT **replaces every record** of that (type, name) |
| Delete | supported | no true delete — rewrite the remainder; the last record of a type cannot be removed |
| Propagation | `ChangeInfo` → `INSYNC`, then authoritative probe | authoritative probe only |

Getting TXT quoting wrong breaks SES verification *and* ACME validation with no
error anywhere — hence the dedicated adapter test in both directions.

### Credentials are forced, and the gate is central

Route53 rides the process AWS credentials. Any other provider must bring its
own, held in `DnsCredential` — not on the `Domain` row, because one provider
account holds many domains and rotation has to happen in exactly one place.

`services/dns.get_adapter()` is the single fail-closed gate: a `godaddy` domain
whose credential is null, inactive, or unverified raises **before any network
call**. Call sites do not repeat this check.

A **tenant's** provider key is account-wide, so blast radius is deliberately
contained: nothing in this app exposes "list every domain in a linked
credential's account". Ownership is proven per-name at registration, so a member
can only ever confirm a domain they already knew to name.

There is exactly one account-wide listing, and it is a different account:
`onboarding.discover_house_domains()` lists the **house AWS account**, which the
platform owns outright and already hands zones out of via `adopt_route53`. It
never touches a `DnsCredential`, so no tenant key is enumerated by it, and its
REST gate is platform superuser (`rest/gates.require_platform_admin`). The BYO
rule above is unchanged and still pinned by a test.

## Permissions

`view_dns` / `manage_dns`, with **`security`** as the domain-category umbrella
(`.claude/rules/models.md` requires the category alongside fine-grained perms).

`DOMAIN_CATEGORIES` in `mojo/helpers/perms.py` is deliberately **not** widened
with a `dns` entry — a bare category term expands to view+manage platform-wide,
and widening that set casually is a known bug pattern in this codebase.

Models with a `group` FK get native group scoping automatically
(`mojo/models/rest.py` keys on `hasattr(cls, "group")`). `Certificate` has no
direct group and declares `GROUP_FIELD = "domain__group"`.

**`services/*.py` perform no permission checks.** Gating lives entirely in
`rest/`. This is what lets the certificate service plant challenge records with
no user in scope, and it means every custom REST handler must call
`rest_check_permission_or_raise` itself — `@md.uses_model_security` does not
gate a custom pk-fetching endpoint.

## Domain lifecycle

Three ways in, and no bare create route (`CAN_CREATE = False`):

1. **Purchase** — `registrar.quote()` then `registrar.purchase()`
2. **Adopt** — `onboarding.adopt_route53()`, an existing house-account zone,
   no money. Superuser-only at the REST layer: adoption hands a group control
   of a zone in the house account, which for anyone else would be a
   cross-tenant zone-claim primitive.
3. **BYO** — `onboarding.register_existing()`, proven by the linked credential

### Finding what to adopt

Adoption is exact-name only, so it can only ever bring in a domain somebody
already remembered. `onboarding.discover_house_domains()` is the inventory that
makes it usable: it lists the house AWS account's **registrations** and **hosted
zones** — two separate APIs whose sets do not match — merged into one row per
name, each flagged `registered` / `hosted_zone` / `tracked` / `adoptable`.

It creates nothing. Ingest stays an explicit `adopt` call, never a side effect
of looking.

Two things it deliberately refuses to hide:

- **`truncated`** — the page walk is bounded (`max_pages`), and a bounded walk
  that reports as complete is a wrong answer, not a small one.
- **Private zones** — VPC-internal, so they resolve nowhere public and are
  excluded. A name whose *only* zone is private still appears when it is also
  registered, flagged un-adoptable, because `find_zone_id` would otherwise fall
  back to that private zone. `adopt_route53` refuses it at the other end too.

### Group assignment

A domain adopted with `group=None` is **platform-scoped**: no tenant can list or
fetch it (the framework filters group-scoped lists to the caller's groups, and a
null group falls through to a check on the caller's *global* permissions, which
a `GroupMember` grant is not).

`registrar/assign-group` moves such a domain into a group. It is superuser-only
and **assign-only** — a domain that already has a group is refused, because
re-homing between tenants is a cross-tenant transfer primitive that nothing
needs.

> **Rows that hang off a house domain need their own guard.** `Certificate`
> scopes through `RestMeta.GROUP_FIELD = "domain__group"`, and the framework
> skips its `request.group` rebind when that path resolves to `None` — so a
> caller-supplied `?group=` survives into a membership check that honors
> `GroupMember` grants. A direct `group` FK does not behave this way (it rebinds
> to `None` and falls through to global permissions). `rest/certificate.py`
> therefore guards its per-instance routes explicitly via
> `_guard_house_certificate`. Any future model scoped through a `GROUP_FIELD`
> path needs the same treatment.

### The no-failed-rows invariant

**A `failed` Domain row never persists.** Every failure path deletes it and
leaves `DomainPurchase` as the audit trail.

This exists because `Domain.name` is unique. If failed rows survived, a
registration that died halfway would permanently poison that name — and with
`CAN_CREATE=False` there would be no supported way to clear it. Keeping the
ledger separate from the live row means the unique constraint only ever guards
live registrations.

## The money path

Purchasing ships **disabled** (`DNSMAN_PURCHASE_ENABLED` defaults to `False`)
and has no single-call path. The ordering in `registrar.purchase()` is
load-bearing:

1. `transaction.atomic()` + `select_for_update()` on the purchase row; verify
   the token hash, the TTL, and `status == "quoted"` **as a compare-and-swap
   under the lock** — so a concurrent second confirm loses cleanly.
2. Create `Domain(status="registering")` **inside** the transaction, so a
   unique-name collision fires *before* any money moves.
3. Mark the purchase `submitted`. **Commit.**
4. *Only then* call the registrar; store the returned operation id.

Step 4 is outside the transaction on purpose. It leaves exactly one awkward
state — `submitted` with no `operation_id` — and that state is *recoverable*:
`poll_pending()` probes the registrar's operation list for a matching
registration, adopts it if found, and fails the row after 30 minutes if not.
The alternative ordering (register, then persist) has an unrecoverable state:
money spent, nothing recorded.

`poll_pending()` is idempotent and status-guarded, so observing a completed
operation twice cannot double-flip a row.

## Certificates

Issued over **ACME DNS-01**, which needs only a TXT record — so it works
identically for both providers and needs no webserver. Issuance and renewal run
centrally as jobs, never on a serving box.

The private key is KMS-envelope-encrypted (`KSMSecrets`) and leaves the database
through exactly one gated, access-logged endpoint. It is in no graph and in no
job payload.

Two details that break naive implementations:

- **A wildcard and its apex share one `_acme-challenge` record name** and their
  two digests must be present *simultaneously*. Challenges are grouped by record
  name and written once with the full value list. Writing them one at a time
  makes the second write erase the first.
- **Challenge cleanup runs in a `finally`** — on success and on failure. Orphaned
  challenge TXT records otherwise accumulate in customer zones.

`DNSMAN_ACME_DIRECTORY_URL` defaults to Let's Encrypt **staging**, so an
unconfigured deployment cannot burn production rate limits. Going live is a
deliberate settings change.

### Why hosts pull instead of being pushed to

On success dnsman broadcasts a `certificate_updated` job carrying only the
certificate id, domain and expiry. Subscribed hosts fetch the material
themselves and reload locally.

The payoff: replacing a serving host is a *sync*, not a reissue — no new CA
order, no rate-limit exposure, and hosts stay disposable.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `DNSMAN_PURCHASE_ENABLED` | `False` | Global kill switch for any real-money call |
| `DNSMAN_MAX_DOMAIN_PRICE` | `50.00` | Refuse quotes above this |
| `DNSMAN_QUOTE_TTL_MINUTES` | `15` | Quote/confirm-token lifetime |
| `DNSMAN_SEARCH_BATCH_LIMIT` | `10` | Max names per batch search, after dedupe |
| `ROUTE53_PRICE_CACHE_HOURS` | `24` | Per-TLD price cache in the route53 helper; `<= 0` disables |
| `DNSMAN_REGISTRANT_CONTACT` | `{}` | ICANN contact; purchase refuses when incomplete. **DB-backed and group-scopable** — edit via `/api/dnsman/registrant`, not the conf file ([details](Registrar.md#the-registrant-contact)) |
| `DNSMAN_ALLOWED_RECORD_TYPES` | A, AAAA, CNAME, TXT, MX, SRV, CAA, NS | Apex NS/SOA still refused |
| `DNSMAN_ACME_DIRECTORY_URL` | Let's Encrypt **staging** | Deliberately not production |
| `DNSMAN_ACME_CONTACT_EMAIL` | `None` | ACME account contact |
| `DNSMAN_CERT_RENEW_DAYS` | `30` | Renew when fewer days remain |
| `DNSMAN_CERT_SYNC_CHANNEL` | `"certs"` | Channel for the cert-updated broadcast |
| `DNSMAN_DNS_PROPAGATION_TIMEOUT` | `300` | Seconds to wait for authoritative visibility |
| `DNSMAN_ACME_HUB_ZONE` | unset | Enables the optional delegated DNS-01 hub; file-only |
| `DNSMAN_ACME_HUB_HOSTED_ZONE_ID` | unset | Optional exact public Route53 zone id; file-only |
| `DNSMAN_ACME_HUB_TTL` | `60` | Hub TXT TTL (bounded); file-only |
| `DNSMAN_ACME_HUB_LEASE_SECONDS` | `900` | Hub challenge lease lifetime (bounded); file-only |
| `DNSMAN_ACME_HUB_PROPAGATION_TIMEOUT` | `300` | Hub Route53/authority timeout (bounded); file-only |
| `DNSMAN_ACME_HUB_PROPAGATION_INTERVAL` | `5` | Hub propagation polling interval (bounded); file-only |
| `DNSMAN_ACME_HUB_SWEEP_LIMIT` | `100` | Max allocations reconciled per sweep; file-only |

`jobs.publish` routes to the channel it is given, so the broadcast always lands
on this channel. `certs` (the default) is in `JOBS_CHANNELS`' default list and is
consumed out of the box; if you set `JOBS_CHANNELS` explicitly — or override
`DNSMAN_CERT_SYNC_CHANNEL` — make sure some engine consumes it, or the broadcast
sits on a queue nobody reads (which raises a `jobs:unconsumed_channel` incident).

## Further reading

- [Registrar.md](Registrar.md) — purchase internals and the ops runbook
- [Providers.md](Providers.md) — adapter interface and provider differences
- [Certificates.md](Certificates.md) — ACME flow, custody, renewal, sync
- [AcmeFederation.md](AcmeFederation.md) — optional protected downstream DNS-01 delegation hub
- [EmailSetupAudit.md](EmailSetupAudit.md) — audit of the pre-existing email path
