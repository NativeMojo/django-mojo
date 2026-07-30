# dnsman — Domains, DNS, WHOIS and TLS certificates

REST reference for `mojo.apps.dnsman`. Base prefix: `/api/dnsman`.

dnsman is the **mechanism** layer: it buys domains, manages DNS records across
providers, manages WHOIS/privacy, and issues and holds TLS certificates. It
carries no end-user purchase policy — spend caps, allowlists, credits and
confirm UX belong to the product built on top of it.

## Permissions

Every endpoint is fail-closed. Two fine-grained permissions plus the `security`
domain category:

| Permission | Grants |
|---|---|
| `view_dns` | read domains, credentials, purchase history, certificate status |
| `manage_dns` | everything above plus writes: DNS records, WHOIS, purchase, certificates |
| `security` | domain-category umbrella — satisfies both |

Rows carrying a `group` are tenant-scoped automatically: pass `?group=<id>`.
A caller never sees another group's rows, and `?group=` can only narrow what
they may already see — it never widens it.

Three surfaces are deliberately stricter than a plain read. **Anything building
UI must gate on these, or it renders controls that 403.**

- `registrar/adopt`, `registrar/discover`, `registrar/assign-group` —
  **platform superuser only**. This is checked as the literal `is_superuser`
  attribute on the user, **not** as a permission string: there is no `admin`
  permission that opens it, and gating a button on one would render a control
  that fails. Passing `?group=` you *do* have rights in does not help either —
  the superuser check runs first, on purpose. These three hand out, enumerate,
  or reassign house-account assets; open to any `manage_dns` holder they would
  be cross-tenant primitives.

  **An API key can never satisfy these**, including a key linked to a superuser
  member. A group-scoped credential must not inherit platform authority, so
  these are interactive-superuser only.
- `certificate/material/<pk>` — requires `manage_dns`, not `view_dns`. Being
  allowed to see that a certificate exists is not the same as being allowed to
  hold its private key. For a domain with **no group** (a house/platform
  domain) it additionally requires a superuser — and so do `GET
  /api/dnsman/certificate/<pk>` and `POST /api/dnsman/certificate/revoke` for
  a certificate on that same domain. Only the per-instance routes carry this
  guard; the list route (`GET /api/dnsman/certificate`) is unaffected.
- **`GET /api/dnsman/whois`** — requires `manage_dns` despite being a read. The
  registrar returns the real registrant name, street address, phone and email to
  the account owner *regardless of WHOIS privacy*, so this is PII and a
  read-only `view_dns` operator has no business seeing it. **Hide the WHOIS
  section entirely from read-only operators** rather than letting it 403.

## Domains

### `GET /api/dnsman/domain` · `GET /api/dnsman/domain/<pk>`
List or fetch domains. Standard list params (`search`, `sort`, paging, `graph`).

### `POST /api/dnsman/domain/<pk>`
Update. Only `auto_renew`, `privacy`, `credential` and `metadata` are writable —
everything else is server-owned. Changing `auto_renew` or `privacy` on a
Route53-backed domain is pushed to the registrar; if that push fails the change
is still saved and the reason appears in `last_error`.

**There is no create route.** Domains come into existence only through
`registrar/quote` → `registrar/purchase`, `registrar/adopt`, or
`registrar/register-existing`.

`group` is **not** writable here. A domain with no group is platform-scoped and
invisible to tenants; assigning it to one is a superuser action through
`registrar/assign-group`, and it cannot be moved again afterwards.

`status` is one of `pending`, `registering`, `active`, `failed`. In practice you
will never see `failed`: a registration that fails deletes its domain row and
leaves the failure on the purchase record instead.

## DNS records

Records are **not mirrored** in our database — these endpoints read and write
the provider's zone live, so there is no drift to reconcile.

### `GET /api/dnsman/dns?domain=<pk>`
```json
{ "domain": "example.com", "provider": "route53",
  "records": [ { "type": "A", "name": "www.example.com",
                 "record_values": ["203.0.113.10"], "ttl": 300 } ] }
```
The value field is `record_values` (a list, always — even for a single value).

### `POST /api/dnsman/dns`
```json
{ "domain": 12, "type": "TXT", "name": "_acme-challenge",
  "record_values": ["abc", "def"], "ttl": 300 }
```
`name` may be relative (`www`), `@` for the apex, or a full FQDN — all three
normalize to the same record. Provider quirks (TXT quoting, apex form) are
handled server-side.

Refused with a clear reason: record types outside the allowed list, apex `NS`
and `SOA`, any name outside the domain's own zone, and any domain that is not
`active`.

### `POST /api/dnsman/dns/delete`
Same addressing. Omit `record_values` to remove the whole record set.

> **GoDaddy limitation:** the provider API has no true delete — removing a value
> means rewriting the remaining ones. Deleting the *last* record of a type is
> rejected by GoDaddy, and you will get an explicit error saying so rather than
> a silent no-op.
>
> One consequence is visible when you list records on a GoDaddy domain. Because
> the last record of a type cannot be removed, certificate issuance cannot delete
> the `_acme-challenge` TXT it planted — it overwrites it with a single
> placeholder value (`retired`) instead. A `_acme-challenge` TXT holding exactly
> that one value is spent and inert; it is not a live challenge and needs no
> action. Route53 domains have the record removed outright.

## Credentials (bring your own provider account)

### `POST /api/dnsman/credential/link`
```json
{ "provider": "godaddy", "api_key": "...", "api_secret": "...", "name": "Acme GoDaddy" }
```
The credential is verified against the provider before anything is stored — a
failed first link persists nothing. Pass `credential: <pk>` to rotate an
existing one in place; the new pair must verify before it replaces the old.

Responses expose only masked values (`api_key_masked`, `api_secret_masked`).
The secret is never returned by any endpoint, in any graph, ever.

A provider key is account-wide, so **no endpoint lists the domains in a linked
account**. Ownership is proven per-name at registration time, which means you
can only ever confirm a domain you already knew to name.

### `POST /api/dnsman/registrar/register-existing`
```json
{ "group": 4, "domain": "example.com", "credential": 7 }
```
Claims a domain you already hold at the provider. The linked credential is the
proof of control: we ask the provider whether that account actually holds this
specific domain and that it is active.

## Capability discovery

### `GET /api/dnsman/config`
Requires `view_dns`, no group. Report what's currently turned on before
attempting a gated action — a client should render its purchase and cert UI
from this, not from probing `registrar/quote` and reading the refusal.

```json
{ "purchase_enabled": false, "registrant_contact_configured": true,
  "max_domain_price": "50.00", "currency": "USD", "quote_ttl_minutes": 15,
  "allowed_record_types": ["A","AAAA","CAA","CNAME","MX","NS","SRV","TXT"],
  "search_batch_limit": 10, "suggestions_enabled": true,
  "providers": [
    { "name": "route53", "purchase": true,  "requires_credential": false },
    { "name": "godaddy", "purchase": false, "requires_credential": true }
  ],
  "acme": { "configured": true, "staging": true },
  "cert_renew_days": 30 }
```

`registrant_contact_configured` is a boolean — the registrant contact itself
is PII and is never returned here or anywhere else. `acme.staging` matters
beyond purchasing: dnsman defaults to Let's Encrypt **staging**, and a
staging-issued certificate is **not publicly trusted** — do not render a
staging cert as "active" without surfacing that. `search_batch_limit` mirrors
`DNSMAN_SEARCH_BATCH_LIMIT`, the cap enforced on batch `registrar/search`
calls. `suggestions_enabled` is always `true` today — there is no kill switch
for it; the flag exists purely so a client can feature-detect batch search
and `registrar/suggest` against an older backend that predates them.

## Buying a domain

Purchasing moves real money and is irreversible. It ships **disabled** — the
`DNSMAN_PURCHASE_ENABLED` kill switch defaults to off, reported at
`config.purchase_enabled` — and there is deliberately **no single-call
purchase path**.

### `POST /api/dnsman/registrar/search`

One name — the flat response, unchanged:
```json
{ "domain": "example.com" }
```
```json
{ "name": "example.com", "status": "AVAILABLE", "available": true,
  "price": 12.00, "currency": "USD", "tld": "com", "tld_supported": true,
  "privacy_supported": true, "reason": null }
```

Batch — either shape (never mixed), answered as `{ "results": [...] }` with
one row per name **in request order**, each row exactly the single-name
object above:
```json
{ "domain": "nativemojo", "tlds": ["com", "dev", "io", "app"] }
```
```json
{ "domains": ["nativemojo.com", "nativemojo.dev"] }
```
- Send one shape or the other. Mixing them — `tlds` together with `domains`,
  or `domain` together with `domains` — is refused with `400` before
  anything is checked.
- The deduped list is capped at `config.search_batch_limit` (default 10);
  over the cap is a `400` and nothing is checked.
- With `tlds` the base may carry its own TLD or not — `"nativemojo.com"`
  and `"nativemojo"` produce the same grid.
- **One bad name never fails the batch.** A name that fails validation
  (e.g. invalid for its TLD) or errors comes back as its own row with
  `available: null` and the explanation in `reason`; its siblings still get
  real answers and the batch is a `200`. The single-name form keeps
  answering `400` for an invalid name, unchanged.

`available` is **tri-state** in every row. `true` and `false` mean what you
expect; **`null` means there is no answer** (registry `PENDING`/`DONT_KNOW`,
or a per-name failure inside a batch — `reason` says which) — retry, and do
not present it to a user as unavailable. `tld_supported: false` means the
registrar does not sell that TLD at all.

### `POST /api/dnsman/registrar/suggest`
```json
{ "domain": "nativemojo.com", "count": 10, "only_available": true }
```
Alternate-name ideas from the registrar, answered as `{ "results": [...] }`
with rows in exactly the search shape — render one grid for both. `count`
defaults to `10` and is clamped to the range `1`–`25` rather than rejected
(a non-numeric `count` is still a clean `400`); `only_available` defaults
`true`. The registry returns no price on suggestions, so `price` is filled
from the server's per-TLD price cache; a TLD the registrar does not sell
keeps its row with `tld_supported: false` and `price: null` rather than
being dropped. If the registrar call itself fails (throttle, missing IAM
grant), the endpoint answers a clean `400` asking to retry — never provider
internals. Requires `view_dns`, like search.

### `POST /api/dnsman/registrar/quote` → step 1 of 2
```json
{ "group": 4, "domain": "example.com", "years": 1 }
```
Returns the price and a single-use `confirm_token` with a short expiry. **The
raw token is shown exactly once and is never retrievable again** — we store only
its hash.

A quote is refused (without creating anything) when purchasing is disabled, the
registrant contact is not configured, the TLD is unsupported, the name is
already tracked, availability is not a definite yes, or the price exceeds the
configured cap.

### `POST /api/dnsman/registrar/purchase` → step 2 of 2
```json
{ "group": 4, "purchase": 31, "confirm_token": "..." }
```
Spends money. A quote can be redeemed exactly once — a second attempt gets a
uniform `400` that deliberately does not say which check failed.

Registration is asynchronous at the registrar (minutes). The purchase row moves
`quoted → submitted → completed`, and the domain becomes `active` when the
registrar confirms. Poll `GET /api/dnsman/purchase/<pk>`.

If registration fails, the purchase row records the error and the domain row is
removed — so a failed attempt never blocks a later retry of the same name.

### `GET /api/dnsman/purchase` · `GET /api/dnsman/purchase/<pk>`
The purchase ledger: who bought what, price, our cost, status, registrar
operation id, and any error. Read-only. `confirm_token` is never returned.

### `GET /api/dnsman/registrar/discover`
Superuser only. Everything the **house AWS account** holds, whether or not
dnsman tracks it — the answer to "what do we own that isn't in here?". Creates
nothing, changes nothing, spends nothing.

Optional `?untracked=1` returns only the rows dnsman does not already have.

```json
{ "count": 2, "truncated": false, "domains": [
  { "name": "example.com", "registered": true, "hosted_zone": true,
    "hosted_zone_id": "Z1ABCDEF", "record_count": 14,
    "expires": "2027-03-01T00:00:00Z", "auto_renew": true,
    "tracked": false, "domain": null, "adoptable": true, "reason": null },
  { "name": "legacy.com", "registered": true, "hosted_zone": false,
    "hosted_zone_id": null, "record_count": null,
    "expires": "2026-11-02T00:00:00Z", "auto_renew": false,
    "tracked": true, "domain": 12, "adoptable": false,
    "reason": "already tracked by this system" }
]}
```

Route53 registers domains and hosts DNS zones through two separate APIs and the
sets do not match, so `registered` and `hosted_zone` are independent: a name can
be one, the other, or both.

- **`tracked`** means *some* `Domain` row already has this name. `Domain.name` is
  globally unique, so a name held as a GoDaddy BYO domain reads as tracked here
  too — this flag is not provider-aware, and `adopt` would refuse the name.
- **`adoptable: false`** — don't offer an adopt control. `reason` says why:
  already tracked, the name will not normalize, or its only hosted zone is
  private (VPC-internal, resolves nowhere public).
- **`truncated: true`** means the page bound was reached and **the list is
  incomplete**. Surface it; do not render a partial inventory as the whole
  account.

### `POST /api/dnsman/registrar/adopt`
Superuser only. Brings an existing house-account hosted zone under management
with no purchase and no money — the path by which DNS management is useful on
day one, before anything has been bought.

```json
{ "domain": "example.com", "create_zone": false }
```

`group` is **optional**. Omit it and the domain is adopted *platform-scoped* —
it belongs to no tenant, appears in no tenant's list, and is assigned later with
`registrar/assign-group`. That is the normal flow out of `discover`.

Supplying a `group` that does not resolve (deleted, or deactivated) is an
**error**, not a silent platform-scoped adopt — so a typo'd group id fails loudly
instead of quietly producing a house domain.

Refused when the name's only hosted zone is private.

### `POST /api/dnsman/registrar/assign-group`
Superuser only. Hands a platform-scoped domain to a group.

```json
{ "domain": 12, "group": 3 }
```

**Assign only.** A domain that already belongs to a group is refused — moving a
domain from one tenant to another is not supported. Build the UI as a one-time
action on unassigned domains, not as an editable field.

## WHOIS and privacy

**All three of these require `manage_dns` — including the GET.** See the
permissions section above: the registrar hands back real registrant PII to the
account owner whether or not privacy is enabled, so there is no read-only tier
here.

Route53-backed domains only. A GoDaddy-backed domain gets an explicit
"management-only" error, not a generic denial.

- `GET /api/dnsman/whois?domain=<pk>` — registrar-held contacts and privacy state
- `POST /api/dnsman/whois` — `{ "domain": 12, "contact": { ... } }`
- `POST /api/dnsman/whois/privacy` — `{ "domain": 12, "enabled": true }`

Privacy is on by default. A few TLDs forbid it (`.us` among them); those are
refused up front with a reason naming the TLD, rather than appearing to succeed.

## Certificates

Certificates are issued centrally over ACME DNS-01 and held here. Because
DNS-01 only needs a TXT record, this works for **both** providers.

### `POST /api/dnsman/certificate/request`
```json
{ "domain": 12, "names": ["example.com", "*.example.com"] }
```
Defaults to the apex plus its wildcard. Issuance runs as a background job and
takes minutes — this returns immediately with a `pending` certificate.

### `GET /api/dnsman/certificate` · `GET /api/dnsman/certificate/<pk>`
Status, SANs, issuer, serial, validity window, `renew_after`, and
`days_remaining`. **These responses carry no PEM and no key.**

`status`: `pending` → `issuing` → `active`, or `failed` (see `last_error`) or
`revoked`.

### `GET /api/dnsman/certificate/material/<pk>`
The only way to obtain key material. Requires `manage_dns`; every release is
logged.

```json
{ "id": 3, "common_name": "example.com", "sans": ["example.com", "*.example.com"],
  "not_after": "...", "cert_pem": "...", "chain_pem": "...", "private_key_pem": "..." }
```

A `503` here means the custody layer (KMS) is temporarily unavailable — **not**
that the certificate has no key. Retry; do not reissue.

### Keeping a serving host in sync

Hosts do not get pushed to. On every successful issue or renewal dnsman
broadcasts a `certificate_updated` job on the configured channel carrying
**only** the certificate id, domain and expiry — never key material. A host
subscribed to that channel calls the material endpoint with its own API key and
reloads locally.

The practical consequence: standing up a replacement server is a sync, not a
reissue. No new certificate order, no CA rate-limit exposure, and servers stay
disposable.

## Error shapes

Standard mojo error envelope. Notable cases:

| Situation | Response |
|---|---|
| Missing/insufficient permission, or another tenant's row | uniform `403`, no detail about which check failed |
| Quote redeemed twice, expired, or bad token | uniform `400` "order not confirmable" |
| Purchasing disabled | `400` naming the kill switch |
| Price over cap | `400` naming the price and the limit (actionable, leaks nothing) |
| GoDaddy domain sent to a registrar-only endpoint | `400` "management-only" |
| Certificate material while KMS is down | `503` "temporarily unavailable" |
