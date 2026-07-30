# Registrar — purchase internals and ops runbook

`mojo/apps/dnsman/services/registrar.py`. Registrar is **AWS Route53 Domains**.
GoDaddy-backed domains are management-only and refuse registrar operations with
an explicit message, not a generic denial.

The `route53domains` API client is **us-east-1 only** — `route53.py` hardcodes
that region and ignores the configured one.

## Surface

```python
search(name)                              # availability + live pricing; creates nothing
search_batch(domain=None, domains=None,   # one grid call: base+tlds or full names;
             tlds=None)                   #   rows share search()'s exact shape
suggest(name, count=10,                   # GetDomainSuggestions + cached per-TLD prices
        only_available=True)
quote(group, user, name, years=1)         # -> price + single-use confirm token
purchase(group, user, purchase_id, token) # the one irreversible money call
poll_pending()                            # advance in-flight registrations; reconcile
get_contacts(domain) / update_contacts(domain, contact) / set_privacy(domain, enabled)
```

None of these check permissions — the REST layer gates every entry point, and
`group`/`user` here are attribution, not authorization.

## Tri-state availability

`check_availability` returns `available` as `True`, `False`, or **`None`**.

`None` is real registry behaviour (`PENDING`, `DONT_KNOW`) on some TLDs, and it
is *not* "unavailable". Collapsing it to `False` would tell a user a name they
could buy is taken. A quote on an indeterminate answer creates **no row at all**
and asks the caller to retry.

`INVALID_NAME_FOR_TLD` is a validation error. A `list_prices` miss means the
registrar does not sell that TLD (`tld_supported: false`) and never raises.

Inside `search_batch`, a name that fails validation or errors becomes its own
row with `available=None` and the explanation in `reason` — per-name
isolation, never a failed batch, and `None` still never reads as "taken". The
fan-out runs on a fixed 4-worker pool with the deduped list capped by
`DNSMAN_SEARCH_BATCH_LIMIT` (default 10): `route53domains` throttles around
5 TPS, and heavier concurrency mostly buys more `PENDING`/`DONT_KNOW`
answers. `suggest` rows reuse the same shape, with prices filled per TLD.

## The price cache

Per-TLD pricing is cached in the route53 helper (`ROUTE53_PRICE_CACHE_HOURS`,
default 24, `<= 0` disables): registry pricing is effectively static, and
without the cache every availability check paid a second AWS round-trip just
to re-fetch it. Only real AWS answers are cached — the failure path never is,
so a transient error cannot pin a TLD `tld_supported=False` for a whole TTL —
and both stores and hits are copies, so no caller can mutate a cached entry.

Two deliberate boundaries: **`quote()` passes `use_cache=False`** — the quoted
price is checked against `DNSMAN_MAX_DOMAIN_PRICE` and written to the ledger,
and no money decision may ride an answer up to a TTL stale (a registry
repricing inside the window would otherwise quote under the cap and register
over it). And a `list_prices` call carrying **explicit credentials** neither
reads nor stores: the cache is keyed on TLD alone, so it may only ever hold
house-account answers.

`suggest` needs the `route53domains:GetDomainSuggestions` IAM action — a
policy scoped to the check/list/register calls will fail it. The failure is
logged in full and surfaced to the caller as a clean retry message, never the
botocore text (which carries the AWS account id and principal).

## Why the purchase ordering is what it is

```
atomic + select_for_update on the purchase row
    verify token hash, TTL, and status == "quoted"   <- compare-and-swap
    create Domain(status="registering")              <- unique-name check fires here
    purchase.status = "submitted"
commit
    route53.register(...)                            <- money moves
    purchase.operation_id = result.operation_id
```

Two failure modes drove this shape:

- **Concurrent confirms.** A read-then-act status check lets two requests both
  pass and both register. The status check is a compare-and-swap *under the row
  lock*, so exactly one wins; the loser gets a uniform 400 that does not reveal
  which check failed.
- **Crashing mid-purchase.** Registering first and persisting after leaves an
  unrecoverable state — money spent, nothing recorded. Persisting first leaves a
  *recoverable* one: `submitted` with no `operation_id`.

`poll_pending()` closes that window by probing `list_operations(submitted_since=…)`
for a `REGISTER_DOMAIN` matching the name. Found → adopt the id and continue.
Nothing after 30 minutes → fail the row, delete the Domain, log an error for ops.

Creating the Domain *inside* the transaction matters too: the unique-name
collision then fires before any money moves, rather than after.

### The no-failed-rows invariant

Every failure path deletes the Domain row; `DomainPurchase` keeps the record.
`Domain.name` is unique, so surviving failed rows would permanently poison a
name — and with `CAN_CREATE=False` there is no supported way to clear one.

## Privacy

WHOIS privacy is on by default and free. AWS exposes **no API** for which TLDs
support it, so `route53.TLDS_WITHOUT_PRIVACY` is a hand-curated, explicitly
best-effort list.

The safety net is in `route53.register()`: if AWS rejects privacy for a TLD not
on our list, it retries **once** without privacy and returns
`privacy_downgraded=True`. The Domain row records the privacy actually applied.
A registration must not fail because our list went stale — but the row must
never claim privacy it does not have.

`set_privacy` capability-gates only *enabling*. Refusing to disable privacy on a
TLD that cannot have it would be nonsense.

## Ops runbook

### Enabling purchases

1. Set the **house registrant contact** — a complete ICANN contact. This is
   portal-managed, not a deploy: `POST /api/dnsman/registrant` with no `group`,
   as a platform admin. Quotes refuse while it is incomplete.
2. Confirm the AWS credentials can reach `route53domains` in **us-east-1**.
3. Review `DNSMAN_MAX_DOMAIN_PRICE` (default 50.00).
4. Set `DNSMAN_PURCHASE_ENABLED = True`.

### The registrant contact

Stored as a `Setting` row under `DNSMAN_REGISTRANT_CONTACT`, resolved through
the normal settings chain: **the group's own row → its parent chain → the
global row → the deployment's conf file**. So a group that sets its own contact
registers under it, and one that does not inherits the house contact. A
deployment that still sets the key in `django.conf` keeps working untouched
until someone saves a DB row over it.

Required fields: `FirstName`, `LastName`, `ContactType`, `AddressLine1`,
`City`, `CountryCode`, `ZipCode`, `PhoneNumber`, `Email`, plus `State` for
US/CA. Shape is checked too, not just presence — `ContactType` against the AWS
enum (`PERSON`, `COMPANY`, `ASSOCIATION`, `PUBLIC_BODY`, `RESELLER`),
`PhoneNumber` against ICANN `+<cc>.<number>`, `CountryCode` as two letters, and
every key against the full AWS `ContactDetail` member list. That last one
matters: botocore raises `ParamValidationError` on an unknown key, so a typo'd
field name in a hand-written setting used to be accepted silently and detonate
at purchase time, after durable intent. It is now a 400 at save time.

`ExtraParams` is accepted for ccTLD registries but its *contents* are not
shape-checked here — AWS validates those.

The row is written with `is_secret=True`, which puts it in `mojo_secrets` and
keeps it out of every REST graph including the unknown-graph fallback. That is
**REST masking, not encryption at rest**: `MojoSecrets` derives the secrets
password from the row's own non-secret columns, so it protects nothing against
someone holding the database.

**Migration note.** A deployment whose conf-file contact has a non-conforming
phone number, an unknown key, or a bad `ContactType` starts refusing quotes
instead of failing at AWS. Fail-closed, and visible:
`registrant_contact_configured` reports `false` and `GET /api/dnsman/registrant`
names the offending field.

### Do the canary purchase first

**Route53 Domains has no sandbox.** There is no way to exercise the real
purchase path without spending money, so the first registration after enabling
must be a cheap house domain, driven through the full quote → purchase → poll
flow. The canary *is* the integration test. Do not let a tenant's purchase be
the first one.

### ICANN registrant-email verification

The first use of a new registrant email triggers an ICANN verification message.
**If it is not verified within 15 days the domain is suspended.** The mailbox in
`DNSMAN_REGISTRANT_CONTACT` must be monitored by a human, not a black hole.

Per-group contacts multiply this, and it is the sharpest operational edge of the
feature. Every tenant that sets its own contact starts its **own** 15-day clock,
on a mailbox **the operator does not monitor and cannot check**. The domain that
gets suspended is one the operator owns and pays for. Before enabling tenant
contacts, decide who chases an unverified registrant email — there is no signal
in this system that will tell you it happened.

### The 60-day transfer lock

ICANN locks a newly registered domain against transfer for **60 days**. This is
registry policy — no API changes it. Anyone promising a customer a transfer-out
inside that window is promising something that cannot be delivered.

### Custody

Custody depends on whose contact was filed, and the answer is no longer always
the operator's.

- **A group with no contact of its own** inherits the house contact. The
  operator is registrant of record and holds the domain on behalf of the
  tenant. That is a real obligation: state it in product-facing terms, honour
  transfer-out on request as a manual process, and do not let it read as
  ownership.
- **A group that sets its own contact** becomes the registrant of record on the
  domains it registers — and, because `route53.register()` sends the one
  contact block as `AdminContact`, `RegistrantContact` **and** `TechContact`,
  also the administrative and technical contact. Those are the addresses that
  receive transfer-approval and change-of-registrant mail. So a tenant
  `manage_dns` holder holds all three roles on an asset the operator owns and
  pays for. This is deliberate; it is not a side effect to discover later.

A quote does **not** pin the contact. `purchase()` re-reads it at confirmation
time, resolving from the **quote's own group** (`row.group`) — never from the
group the confirming request happened to name, which is optional attribution and
is `None` whenever that group went inactive between the two calls. An edit
between quote and confirm therefore changes what gets filed, by design: the TTL
is 15 minutes, the price is unaffected, and refusing a redeemed quote over a
benign contact edit would be worse.

Because the answer is now tenant-editable, the ledger records it.
`DomainPurchase.metadata` carries `registrant_scope` (the group id the contact
was resolved for, or `"house"`) and `registrant_fingerprint`, a SECRET_KEY-salted
SHA-256 of the contact that was actually sent. `metadata` is in neither REST
graph, so this is an audit trail, not a second PII store — and the fingerprint
is not reversible into a contact.

### Routine checks

- `poll_domain_operations` (every 5 min) must be running, or registrations never
  leave `submitted`. It is a **dispatcher**: the cron function publishes
  `asyncjobs.poll_domain_operations` and returns, so the sweep needs a **job
  runner on the `default` channel** as well as the cron trigger. If the cron
  fires but no runner is consuming, jobs pile up and registrations still never
  settle — check both.
- Purchases stuck `submitted` with no `operation_id` past 30 minutes are logged
  as errors — that is the crash-window alarm and it deserves a human.
- Domains have `auto_renew=True` by default; expiries accrue on the house
  account. Watch `Domain.expires`.

### Auditing the house AWS account

`GET /api/dnsman/registrar/discover` (superuser) answers "what is in our AWS
account that dnsman does not know about?" — merged across the registrar and
hosted-zone APIs, one row per name.

```
GET /api/dnsman/registrar/discover?untracked=1
```

Worth running after any manual console work, and before assuming an outage is a
dnsman bug — a domain nobody adopted is a domain nobody is renewing, monitoring
or issuing certificates for.

Reading the flags:

| Flag | Means |
|---|---|
| `registered: false, hosted_zone: true` | Registered elsewhere, DNS hosted here. Adoptable; renewal is somebody else's problem. |
| `registered: true, hosted_zone: false` | We own it, no zone exists. Adopt with `create_zone: true`. |
| `tracked: true` | A `Domain` row already has this name — **any provider**, since `Domain.name` is globally unique. `adopt` will refuse it. |
| `adoptable: false` | Already tracked, or the name will not normalize, or its only zone is private. `reason` says which. |
| `truncated: true` | The page bound was hit. **The list is incomplete** — do not treat it as an inventory. |

Ingest is deliberately two steps: `discover` never creates anything, and each
`adopt` is its own call. Adopt without a `group` for anything that is not yet a
specific tenant's, then `registrar/assign-group` when it is. Assignment is
one-way — a domain that already has a group is never re-homed.
