# Providers — the dispatch layer

`mojo/apps/dnsman/services/dns.py` plus `services/providers/`.

One interface, two back-ends. Everything provider-specific is confined to an
adapter, because the differences fail silently rather than loudly.

## Interface

```python
list_records(domain)                                          # -> [objict(type, name, record_values, ttl)]
upsert_record(domain, rtype, name, record_values, ttl=300)    # -> objict(change_id, provider)
delete_record(domain, rtype, name, record_values=None)        # -> objict(change_id, provider)
clear_record(domain, rtype, name, record_values=None)         # -> objict(change_id, provider)
wait_for_propagation(domain, rtype, name, record_values, timeout=None, change_id=None)  # -> (ok, seen_values)
```

`record_values` is **always a list**, even for a single value.

> The field is `record_values` and not `values` because `objict` subclasses
> `dict`: `record.values` resolves to the bound `dict.values` method and hands
> back a method object. Two independent implementations hit this during the
> original build, one as a live bug. A test pins the name.

Callers always speak **FQDN**. Converting to a provider's preferred form is the
adapter's job and nowhere else's.

`dns.py` is **ungated mechanism** — no permission checks. That is what lets the
certificate service plant challenge records with no user in scope. Gating lives
in `rest/`.

Validation performed before dispatch: domain must be `active`; record type must
be in `DNSMAN_ALLOWED_RECORD_TYPES`; apex `NS`/`SOA` refused; out-of-zone names
refused.

The optional [ACME federation hub](AcmeFederation.md) does not pass through this
dispatch layer. It owns one separately configured public Route53 zone and an
internal exact-target writer that tenants cannot reach through generic record
CRUD. A verified external name gets `Domain.provider="mojo"` solely as a
certificate-only marker; `get_adapter()` deliberately refuses it. Existing
Route53/GoDaddy domains keep their provider value even when a verified sticky
delegation selects the remote certificate writer. Direct DNS and HTTP-01
behavior are unchanged.

The downstream `services/acme_hub_client.py` is equally separate. It exposes
only target allocation plus challenge-lease publish/withdraw against fixed hub
paths; it is not a `DnsProvider`, is not returned by `get_adapter()`, and cannot
accept a caller-selected record name. Missing or failed hub configuration is a
loud typed failure with no provider fallback. Direct Route53/GoDaddy issuance
and Maestro Sites HTTP-01 therefore keep their existing dispatch paths.

## Provider differences

| | Route53 | GoDaddy |
|---|---|---|
| Record name | FQDN | relative label; `@` at the apex |
| TXT values | quoted, chunked at 255 chars | raw |
| Write | true upsert of one record set | PUT **replaces every record** of that (type, name) |
| Delete | supported | no true delete |
| Record read shape | one record **set** per entry | one entry **per value** (JSON array) |
| Propagation signal | `ChangeInfo` → `INSYNC`, then authoritative probe | authoritative probe only |
| Purchase | yes | **no** — management only |

### TXT quoting

Route53 requires each TXT string wrapped in quotes and split at 255 characters;
GoDaddy stores the raw string. Send a raw value to Route53 and SES verification
and ACME validation both fail with nothing in any log to explain it. Use
`route53.format_txt_value()` / `parse_txt_value()`; the adapter tests assert the
round trip in both directions.

### GoDaddy replaces, it does not merge

`edit_record` is a PUT over the whole (type, name) pair. An upsert must
therefore send the **complete desired value list**, not just the new value —
otherwise writing a second TXT digest silently erases the first. This is exactly
the shape ACME needs for a wildcard plus its apex.

### GoDaddy cannot delete the last record of a type

Deletion means rewriting the remaining values, and GoDaddy rejects the empty
replacement. Where a true delete is impossible `delete_record` raises a clear
error. It never silently no-ops — a caller that believes a record is gone when it
is not is worse than a caller that sees a failure.

### `clear_record` — for callers with nowhere to put that refusal

Some callers cannot act on "this provider cannot delete that". Certificate
cleanup is the case that matters: it runs inside a `finally` and swallows, so a
raise there just leaves the `_acme-challenge` TXT live in the customer's zone,
gaining another stale digest on every renewal.

`clear_record` is the same request with a different failure contract — retire
these values, using the strongest removal the provider actually has:

- **Route53** (`can_delete_last_record = True`, the interface default) does
  exactly what `delete_record` does.
- **GoDaddy** removes the named values when others survive, and when they are all
  the set holds, overwrites the set with a **single inert placeholder value**
  (`godaddy_provider.RETIRED_VALUE`) so nothing resolves and GoDaddy still gets
  the non-empty replacement it insists on. A record that does not exist is
  already clear: no placeholder is planted. Only character-string types
  (`TXT`/`SPF`) get a placeholder — inventing an address for an `A` or a target
  for a `CNAME` would point real traffic somewhere, so those still raise.

Use `delete_record` everywhere else. It is the honest answer, and `clear_record`
deliberately trades that honesty for "the data is gone".

### The GoDaddy transport helper

`mojo/helpers/dns/godaddy.py` owns every GoDaddy HTTP call, including URL
building. Two things about it are load-bearing:

- **`build_url()` percent-encodes every path segment.** A record name containing
  path separators otherwise normalizes into a write against a *different domain
  in the same account*. This has been exploitable once already; never assemble a
  GoDaddy path by string interpolation.
- **Array vs object bodies.** `get_domains()`, `get_records()` and
  `get_record()` answer with JSON **arrays** and return a **list of `objict`**;
  `get_domain_info()` answers with an object and returns a single `objict`.
  `objict` subclasses `dict`, so `objict([...])` raises `ValueError` — and
  `objict([])` is merely `{}`, which is why wrapping arrays directly used to look
  fine against an empty zone and fail against every real one. Everything goes
  through `parse_body()`.

`edit_record(domain, rtype, name, data, ttl)` accepts a scalar **or a list** of
values and builds one payload entry per value. `put_records()` is the raw door
for the rare case where surviving values carry different TTLs.

`raise_on_error` defaults to **False** and must stay that way: a sibling repo
consumes this helper and depends on the historical swallow-everything behaviour.
dnsman always builds its managers with `raise_on_error=True`.

## Credentials

Route53 uses the process AWS credentials. GoDaddy brings a `DnsCredential`.
The `mojo` marker needs neither because it has no general DNS adapter.

The gate is central, in `get_adapter()`: a `godaddy` domain whose credential is
null, `is_active=False`, or `verified=False` raises **before any network call**.
Individual call sites do not repeat the check — one gate, one place to audit.

A provider key is account-wide, which is the reason for two deliberate
restrictions:

- No endpoint or service return value ever lists the domains in a linked
  account.
- Ownership is proven **per name** at registration time, so a caller can only
  confirm a domain they already knew to name.

## Adding a third provider

1. Implement the four-method interface in `services/providers/<name>_provider.py`.
2. Normalize names and record values at that boundary — nowhere else.
3. Add the provider to `Domain.provider`'s accepted values and to
   `get_adapter()`.
4. If it needs credentials, it is automatically covered by the existing
   fail-closed gate via `Domain.requires_credential`.
5. Give it a propagation strategy. If it has no change-status API, the
   authoritative probe (`mojo/helpers/dns/probe.py`) is the fallback.
6. If it cannot delete the last value of a record set, set
   `can_delete_last_record = False` and override `clear_record`. The default
   implementation just forwards to `delete_record`, which is correct for anything
   with a real delete.
