# Providers — the dispatch layer

`mojo/apps/dnsman/services/dns.py` plus `services/providers/`.

One interface, two back-ends. Everything provider-specific is confined to an
adapter, because the differences fail silently rather than loudly.

## Interface

```python
list_records(domain)                                          # -> [objict(type, name, record_values, ttl)]
upsert_record(domain, rtype, name, record_values, ttl=300)    # -> objict(change_id, provider)
delete_record(domain, rtype, name, record_values=None)        # -> objict(change_id, provider)
wait_for_propagation(domain, rtype, name, record_values, timeout=None)  # -> (ok, seen_values)
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

## Provider differences

| | Route53 | GoDaddy |
|---|---|---|
| Record name | FQDN | relative label; `@` at the apex |
| TXT values | quoted, chunked at 255 chars | raw |
| Write | true upsert of one record set | PUT **replaces every record** of that (type, name) |
| Delete | supported | no true delete |
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
replacement for some types. Where a true delete is impossible the adapter raises
a clear error. It never silently no-ops — a caller that believes a record is
gone when it is not is worse than a caller that sees a failure.

## Credentials

Route53 uses the process AWS credentials. Everything else must bring its own
`DnsCredential`.

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
