# Registrar — purchase internals and ops runbook

`mojo/apps/dnsman/services/registrar.py`. Registrar is **AWS Route53 Domains**.
GoDaddy-backed domains are management-only and refuse registrar operations with
an explicit message, not a generic denial.

The `route53domains` API client is **us-east-1 only** — `route53.py` hardcodes
that region and ignores the configured one.

## Surface

```python
search(name)                              # availability + live pricing; creates nothing
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

1. Set `DNSMAN_REGISTRANT_CONTACT` — a complete ICANN contact. Required:
   `FirstName`, `LastName`, `ContactType`, `AddressLine1`, `City`,
   `CountryCode`, `ZipCode`, `PhoneNumber`, `Email`, plus `State` for US/CA.
   Quotes refuse while it is incomplete.
2. Confirm the AWS credentials can reach `route53domains` in **us-east-1**.
3. Review `DNSMAN_MAX_DOMAIN_PRICE` (default 50.00).
4. Set `DNSMAN_PURCHASE_ENABLED = True`.

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

### The 60-day transfer lock

ICANN locks a newly registered domain against transfer for **60 days**. This is
registry policy — no API changes it. Anyone promising a customer a transfer-out
inside that window is promising something that cannot be delivered.

### Custody

Domains are registered with the operator's contact as registrant of record and
held on behalf of the tenant. That is a real obligation: state it in
product-facing terms, honour transfer-out on request as a manual process, and do
not let it read as ownership.

### Routine checks

- `poll_domain_operations` (every 5 min) must be running, or registrations never
  leave `submitted`.
- Purchases stuck `submitted` with no `operation_id` past 30 minutes are logged
  as errors — that is the crash-window alarm and it deserves a human.
- Domains have `auto_renew=True` by default; expiries accrue on the house
  account. Watch `Domain.expires`.
