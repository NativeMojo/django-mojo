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
- **`GET /api/dnsman/registrant`** — same reasoning, same rule: requires
  `manage_dns` despite being a read, because the payload *is* the contact PII.
  With no `?group=` it addresses the house contact and additionally requires a
  platform admin. See [the registrant contact](#the-registrant-contact).

The legacy `/acme/delegation` and `/acme/challenge/*` hub endpoints are a separate machine-only surface. They accept
only an `Authorization: apikey …` credential whose underlying key carries the
protected `dnsman_acme_federation` permission and whose project is active. JWT
users, group tokens, anonymous requests, permissionless keys, and acting-user
overrides whose underlying key lacks the permission all receive `403`.

## ACME delegation hub (optional)

The hub is absent by default and has no fallback behavior. A configured
downstream uses a fresh immutable `client_ref` per onboarding:

### `POST /api/dnsman/acme/delegation`

```json
{ "domain": "customer.example", "client_ref": "site-install-2026-08" }
```

```json
{
  "client_ref": "site-install-2026-08",
  "domain": "customer.example",
  "source": "_acme-challenge.customer.example",
  "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net"
}
```

This is the exact `data` object inside the standard success envelope. The same
project/ref/domain is idempotent; reusing the ref for another domain is refused.
Publish the returned source as one CNAME to the returned target.

### `POST /api/dnsman/acme/challenge/publish`

```json
{ "client_ref": "site-install-2026-08", "challenge_ref": "order-123",
  "values": ["digest-for-apex", "digest-for-wildcard"] }
```

`values` is the complete digest set for that challenge. The hub first verifies
the stored source is exactly one CNAME hop to its stored target, then durably
leases the set and replaces the target RRset with the union of all active
leases. A retry must use the same `challenge_ref` and values.

**A `200` means the challenge write was ACCEPTED, not that it is visible.** The
hub returns as soon as the lease is durable and Route53 has accepted the
change; it does not wait for propagation, because your read timeout on this
call is far shorter than DNS convergence takes. Before you ask the CA to
validate, probe the target yourself and require a **majority of its
authoritative nameservers** to serve your digests — one nameserver answering is
not propagation. The response shape is unchanged.

```json
{
  "client_ref": "site-install-2026-08",
  "domain": "customer.example",
  "source": "_acme-challenge.customer.example",
  "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net",
  "challenge_ref": "order-123",
  "active_value_count": 2
}
```

`active_value_count` is the size of the deduplicated TXT union, not a lease
count. Replaying a ref with different values, or republishing an expired or
withdrawn ref, is refused.

### `POST /api/dnsman/acme/challenge/withdraw`

```json
{ "client_ref": "site-install-2026-08", "challenge_ref": "order-123" }
```

Idempotently retires only that lease and preserves values from parallel
challenges. Withdrawal deliberately succeeds after the tenant CNAME has been
removed, so revocation cannot block cleanup.

The response uses the same six-field challenge shape shown for publish, with
the remaining `active_value_count`; an unknown/already-retired reference is a
successful no-op. Hub responses never contain TXT digests, hosted-zone ids,
AWS change ids, credentials, or unrelated allocations.

References are 1–128 safe identifier characters. `values` accepts 1–20 entries
(duplicates are deduplicated), each 1–1024 characters with no NUL/CR/LF. All
three endpoints return 200 on success and share one sliding bucket: 120
requests/minute per IP and 300 per project. A limit response is 429 with
`Retry-After`.

### Downstream client behavior

The consuming deployment configures a file-only hub URL and protected ApiKey;
there is no downstream hub-zone setting because allocation returns the full
opaque target. Missing or invalid configuration makes delegation unavailable
and is never treated as success. A configured client refuses redirects and
malformed/cross-request replies, bounds connect/read timeouts, and retries the
identical idempotent request at most once only for connection/read ambiguity or
HTTP 502/503/504. It does not retry 400/401/403/409/429.

Products must persist the allocation and publish the returned source→target
CNAME before considering delegation verified. For each issuance, persist a
locally generated immutable `challenge_ref` and cleanup intent before publish,
probe the returned target for propagation, and always withdraw that same
reference afterward. A verified or broken delegation never silently falls back
to direct Route53/GoDaddy. Those direct providers and Maestro Sites HTTP-01 are
separate flows and remain unchanged.

### Tenant delegation lifecycle

The consuming dnsman deployment wraps that machine transport in normal
tenant-gated endpoints:

| Endpoint | Permission | Request / behavior |
|---|---|---|
| `POST /api/dnsman/delegation/initiate` | `manage_dns` or `security` | Either `{ "domain": 12 }` or `{ "group": 4, "name": "external.example" }`; returns the permanent CNAME source/target. An external name remains pending and does not reserve `Domain.name`. |
| `GET /api/dnsman/delegation?domain=12` | `view_dns`, `manage_dns`, or `security` | Returns a list (maximum 100, newest first) for that authorized Domain. |
| `GET /api/dnsman/delegation/<id>` | `view_dns`, `manage_dns`, or `security` | Returns one authorized non-retired delegation. |
| `POST /api/dnsman/delegation/verify` | `manage_dns` or `security` | `{ "delegation": 9 }`; proves the exact one-hop alias and creates an external name's certificate-only Domain only after success. |

Every tenant route returns this exact object (the list route returns an array of
them):

```json
{
  "id": 9,
  "created": "2026-08-06T12:00:00+00:00",
  "modified": "2026-08-06T12:03:00+00:00",
  "domain": 12,
  "domain_name": "external.example",
  "source": "_acme-challenge.external.example",
  "target": "4a0f0123456789abcdef0123456789ab.acme-hub.example.net",
  "state": "verified",
  "verified_at": "2026-08-06T12:03:00+00:00",
  "last_error_code": null
}
```

`domain` and `verified_at` are null for an external pending name. Timestamps are
ISO 8601 strings. `last_error_code` is a bounded diagnostic (for example
`alias_lookup_failed`, `alias_mismatch`, `domain_claimed`, `group_inactive`,
`configuration`, `transport`, `response`, `http`, or
`hub_allocation_mismatch`); do not show it as end-user copy.

Public states are `pending`, `verified`, and `broken`; retired tombstones are
never serialized. Pending is inert. After first verification routing is sticky:
a missing/changed/chained alias becomes broken and certificate issuance fails
closed without silently using Route53/GoDaddy. Delegated v1 accepts exactly the
apex-plus-wildcard profile. These endpoints use normal `view_dns`/`manage_dns`
tenant isolation and never return the hub ApiKey, client reference, cleanup
reference, TXT digest, PEM, or private key.

The supported flow for an existing shared target is documented in
[project shared-validation setup](#project-shared-validation-setup). Use that
flow instead of legacy initiate when another project already owns the target.

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

### `GET /api/dnsman/credential/group-choice`

Minimal Group choices for a platform credential-assignment control. This is a
global operator surface: it requires a global `manage_dns` or `security` grant,
or a superuser. A tenant/member-only grant is insufficient, and ApiKeys and
GroupScopedTokens are always refused even when they represent a privileged
user.

List mode accepts only `search`, `start`, and `size`. `search` is trimmed,
case-insensitive, and limited to 100 characters. `start` defaults to 0 and is
bounded at 100000; `size` defaults to 25 and is bounded at 50. Results sort by
case-insensitive name and then id, with `count` describing all matches before
paging:

```json
{ "status": true,
  "data": [{ "id": 4, "name": "Acme" }],
  "start": 0, "size": 25, "count": 1 }
```

Exact mode is `?id=<positive integer>` and cannot be mixed with list controls.
An inactive or missing id returns the same successful empty shape with
`start: 0`, `size: 1`, and `count: 0`. Duplicate parameters, bracket/dotted
shapes, unknown keys, and generic list controls (`sort`, `graph`, etc.) are a
bounded `400`.

Do **not** send `group` on this route. It is reserved for the REST dispatcher,
which may resolve and touch it before route authorization; the choice endpoint
does not accept it as a filter. Portal adapters should send only the four
route-owned parameters above. Each row contains exactly `id` and `name`.

Choices are advisory UI data, not an authorization decision or a reservation.
A group can be deactivated after discovery. The link request below must still
send the chosen `group`; the dispatcher resolves it again at request time and
linking refuses before provider verification or persistence if it is no longer
active.

### `POST /api/dnsman/credential/link`
```json
{ "group": 4, "provider": "godaddy", "api_key": "...", "api_secret": "...",
  "name": "Acme GoDaddy" }
```
The credential is verified against the provider before anything is stored — a
failed first link persists nothing. Pass `credential: <pk>` to rotate an
existing one in place; the new pair must verify before it replaces the old.
`group` remains required on rotation for dispatch-time permission and active-
group checks, but rotation never re-homes the credential: it stays on its
existing group.

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
Requires `view_dns`. Optional `?group=<id>`. Report what's currently turned on
before attempting a gated action — a client should render its purchase and cert
UI from this, not from probing `registrar/quote` and reading the refusal.

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
  "delegated_acme": { "available": false, "record_type": "CNAME",
    "target_suffix": null, "profile": "apex_wildcard",
    "requires_provider_credentials": false },
  "cert_renew_days": 30 }
```

`registrant_contact_configured` is a boolean — the registrant contact itself is
PII and is never returned here. **It is the one field that varies per group:**
pass `?group=<id>` and it answers for that group (its own contact if it has one,
otherwise the one it inherits); omit the group and it answers for the house
account, exactly as before. So a purchase UI scoped to a group must ask with
that group, or it will report the wrong availability. To read or edit the
contact itself, see [the registrant contact](#the-registrant-contact) below.
`acme.staging` matters
beyond purchasing: dnsman defaults to Let's Encrypt **staging**, and a
staging-issued certificate is **not publicly trusted** — do not render a
staging cert as "active" without surfacing that. `search_batch_limit` mirrors
`DNSMAN_SEARCH_BATCH_LIMIT`, the cap enforced on batch `registrar/search`
calls. `suggestions_enabled` is always `true` today — there is no kill switch
for it; the flag exists purely so a client can feature-detect batch search
and `registrar/suggest` against an older backend that predates them.
`delegated_acme.available` is false only when the downstream hub URL and key
are both absent. `target_suffix` is null because the hub assigns an opaque full
target during initiation; clients must use that returned target verbatim. A
partial or unsafe URL/key configuration fails capability discovery instead of
masquerading as `available: false`; that is a deployment error, not a disabled
feature.

## The registrant contact

The ICANN contact domains get registered under. **Per group, with a house
fallback**: a group with its own contact registers under it, a group without one
inherits the house contact. Both live at one path, selected by `?group=`.

### `GET /api/dnsman/registrant` · `POST /api/dnsman/registrant`

Optional `?group=<id>`. **Both verbs require `manage_dns`** — including the
read. The payload is a legal name, street address, phone number and email, so
`view_dns` reaches neither scope; hide the whole section from read-only
operators rather than letting it 403.

**Omitting `group` addresses the HOUSE contact and requires a platform admin**
(interactive superuser — an API key is refused). It is the operator's own
personal data and the registrant of record for every tenant without one. A
tenant admin gets a 403 from this endpoint, and the refusal discloses nothing.

> **This endpoint is not the only way to see a registrant contact.** A domain
> registered under the house contact carries it at the registrar, and
> `GET /api/dnsman/whois?domain=<pk>` returns the registrar-held registrant,
> admin and tech contacts to any `manage_dns` holder on that domain's group —
> AWS hands them to the account owner regardless of WHOIS privacy. So a tenant
> holding a domain that inherited the house contact can read it there. Treat
> the scope isolation here as "the editor does not disclose it", not as a
> guarantee that the house contact is unreachable by tenants.

A `group` that is **supplied but does not resolve** (deleted, deactivated, or a
typo) is a **400** — `"...does not exist or is not active..."` — not a silent
fall-through to the house scope. That distinction matters: falling through
would ask a tenant admin for platform-admin rights they never meant to invoke.

Both verbs return the same body:

```json
{ "scope": "group", "group": 42,
  "contact": { "FirstName": "...", "LastName": "...", "ContactType": "PERSON",
               "AddressLine1": "...", "City": "...", "State": "...",
               "CountryCode": "US", "ZipCode": "...",
               "PhoneNumber": "+1.5035551212", "Email": "..." },
  "source": "database", "inherited": false,
  "effective_configured": true, "problems": [] }
```

`contact`, `source` and `problems` describe **this scope's own row only** —
never an inherited contact. A group with nothing of its own gets
`contact: null`, `source: "none"`, `problems: []`, and `inherited: true`. That
is deliberate: reporting which fields of an inherited contact are malformed
would tell a tenant about the house one.

| Field | Meaning |
|---|---|
| `source` | `database` (a saved row), `settings_file` (global scope only — a conf-file value that saving will shadow), or `none` |
| `inherited` | Group scope with no row of its own, but a contact in effect above it |
| `effective_configured` | Whether a quote would succeed for this scope right now — the same answer as `config.registrant_contact_configured` |
| `problems` | Field-level complaints about **this** scope's row. Never contains a value, only field names |

**POST** takes `{"contact": {...}}` to save, or `{"clear": true}` to drop this
scope's row. Clearing a group reverts it to whatever it inherits; clearing the
global scope reverts to the deployment's conf file when one is set (`source`
then reports `settings_file`).

Validation runs before anything is written, so a contact AWS would bounce is a
readable 400 here rather than a failed registration after money has moved.
Required: `FirstName`, `LastName`, `ContactType`, `AddressLine1`, `City`,
`CountryCode`, `ZipCode`, `PhoneNumber`, `Email`, plus `State` for US/CA. Also
enforced: `ContactType` ∈ `PERSON | COMPANY | ASSOCIATION | PUBLIC_BODY |
RESELLER`, `PhoneNumber` as ICANN `+<cc>.<number>` (e.g. `+1.5035551212`),
`CountryCode` as two letters, and **no unknown keys** — a misspelled field name
is rejected rather than silently accepted. `ExtraParams` is allowed through for
ccTLD registries but its contents are AWS's to validate.

A saved contact takes effect on the next quote with no restart. Note that a
**tenant which sets its own contact holds the registrant, admin and technical
roles** on domains it registers, and that its registrant email starts its own
ICANN 15-day verification clock — surface that in the UI rather than leaving it
to be discovered.

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
{
  "group": 4,
  "purchase": 31,
  "confirm_token": "...",
  "confirm_domain": "example.com",
  "confirm_price": "12.00"
}
```
Spends money. Requires `manage_dns` (or `security`) for the quote's group, a
browser user session authenticated within the last 600 seconds, and the
operator-typed normalized domain and exact quoted decimal price. API keys and
other key-backed sessions receive `403`; stale interactive authentication uses
the standard `440 reauth_required` response. A quote can be redeemed exactly
once — a token, state, expiry, group, typed-domain, or typed-price mismatch gets
the same uniform `400` and makes no registrar call.

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
DNS-01 only needs a TXT record, this works through both direct providers or a
verified delegated target.

### `POST /api/dnsman/certificate/request`
```json
{ "domain": 12, "names": ["example.com", "*.example.com"] }
```
Defaults to the apex plus its wildcard. Issuance runs as a background job and
takes minutes — this returns immediately with a `pending` certificate.
Concurrent identical requests/jobs are deduplicated. For delegated domains the
only accepted names are exactly the apex plus wildcard. A failed delegated
renewal leaves still-valid active material available and schedules a bounded
retry; surface `last_error`, but do not replace a still-active serving cert.

### `GET /api/dnsman/certificate` · `GET /api/dnsman/certificate/<pk>`
Status, SANs, issuer, serial, validity window, `renew_after`, and
`days_remaining`. **These responses carry no PEM and no key.**

`status`: `pending` → `issuing` → `active`, or `failed` (see `last_error`) or
`revoked`.

### `POST /api/dnsman/certificate/remove-failed`
```json
{ "certificate": 7 }
```
Removes an inert failed issuance attempt from the operator inventory. The same
model permissions and house-certificate superuser gate as revocation apply,
and any status other than `failed` is refused. Successful issuance removes
older failed attempts for the same domain and exact name set automatically.
Failure evidence remains in job and application logs.

### `GET /api/dnsman/certificate/retire-eligibility?domain=<pk>`
```json
{ "domain": 12, "eligibility": { "7": 9, "8": null } }
```
For every certificate on the domain, the id of the active certificate that
could take over its duty, or `null`. DB-only — no provider calls — so it's
safe to call on every domain page load. Same permission shape as the domain
route (`VIEW_PERMS`, plus the house-domain platform-admin gate for a
group-less domain).

### `POST /api/dnsman/certificate/retire`
```json
{ "certificate": 7 }
```
Retires a certificate another active certificate on the same domain can fully
replace: every vhost still using it is repointed to the replacement, then the
row is deleted. Refused, naming the reason, when no active certificate covers
every name the target lists, the only one that does is itself due for
renewal, or the replacement cannot serve a specific vhost still enabled on the
target. Same guards as `remove-failed`/revoke — this is destructive. Returns:
```json
{ "retired": 7, "replaced_by": 9, "vhosts_repointed": 2 }
```

### `GET /api/dnsman/certificate/material/<pk>`
The only way to obtain key material. Requires `manage_dns`; every release is
logged.

```json
{ "id": 3, "common_name": "example.com", "sans": ["example.com", "*.example.com"],
  "not_after": "...", "cert_pem": "...", "chain_pem": "...", "private_key_pem": "..." }
```

A `503` here means the custody layer (KMS) is temporarily unavailable — **not**
that the certificate has no key. Retry; do not reissue.
KMS decryption errors retain this response and never return a successful
payload with an empty private key.

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
| Hub endpoint called without an eligible protected ApiKey | `403` |
| Hub input/ref conflict, unknown allocation, retired challenge ref, or failed CNAME proof | `400` |
| Hub rate limit | `429` plus `Retry-After` |
| Hub disabled, wrong/unavailable public zone, or provider failure | `503` |
| Tenant delegation missing/retired | `404` |
| Tenant delegation cross-tenant or missing read/write permission | `403` |
| Tenant initiate shape, alias proof, active-group, or domain-claim failure | `400` |
| Tenant downstream client configuration/allocation transport failure | `503` |

## DNS record-set reservation conflicts

Certificate DNS-01 challenges durably reserve their complete TXT record set.
`POST /api/dnsman/dns` and `POST /api/dnsman/dns/delete` return `400` before a
provider call if the exact `(domain, type, name)` is owned by an in-flight or
cleanup-pending challenge. Refresh the record inventory and retry after the
certificate operation finishes; do not overwrite the challenge with a partial
value set. A provider timeout during challenge publication is reconciled from
the exact authoritative record set. Absent or mismatched inventory is not
success: the server keeps durable attempted intent and reports failure without
treating the write as proven. Clients must not add their own blind retry loop.
Reservation creation and interactive complete-set writes share a stable
per-domain lock, including when no reservation row existed at request start.

## Built-in Admin workflow

The packaged Admin exposes permanent Domains, Credentials, DNS records, and
Certificates pages using exactly these APIs. Availability is permission-gated:
house-account discover/adopt controls appear only for a literal superuser, and
all writes still rely on server authorization.

The DNS page edits complete record sets. It performs at most one provider write
per `(domain, type, name)` intent, never transport-retries that write, and then
reloads authoritative inventory. If the outcome cannot be proved, the set is
marked refresh-required and remains write-locked until the operator refreshes.
Clients implementing another console should preserve that behavior. The
Certificates page polls list/detail metadata only; it does not request PEM or
private-key material. Per-domain certificate management — including
**Retire** — lives on the domain's own detail panel, not the flat Certificates
list: it calls `retire-eligibility` alongside the domain's certificate list to
decide which rows can offer a retire button (only those with a covering
replacement), shows how many addresses move to the replacement before
confirming, and calls `certificate/retire`. The flat list stays read-only
metadata plus `remove-failed` for dead attempts.


## Shared validation v2

All new routes below are `POST /api/dnsman/acme/<path>`, read exact JSON request
shapes and return the normal `{status:true,code:200,data:...}` envelope. They
share the existing strict ACME rate bucket (429 plus Retry-After). Authentication
runs before request validation. A protected active federation ApiKey identifies
its **original** group, ignoring acting users and claimed groups; the group and
ancestors must be active. GroupScopedToken is always refused.

A controller is that original project key for its existing allocation, or a
genuine hub platform superuser using normal session authentication. DNS/security/
Sites managers and key-administration privileges alone cannot grant issuance
trust. A human controller cannot implicitly publish as a recipient.

`allocation_ref` is the normalized existing target FQDN: public, immutable and
never a bearer credential. Lookups, recipient recognition, grant and join never
allocate another target or modify the public CNAME.

| Path | Exact request | Response data / authorization |
|---|---|---|
| `capabilities` | `{}` | `{version:2,shared_validation:boolean,consumer_project_uuid:string|null,can_consume:boolean,can_manage_trust:boolean,trust_scope:"apex_and_wildcard"}`. Protected keys: true/true/own UUID; true human superuser: false/true/null; other authenticated humans: false/false/null. Hub configuration failure is 503. Eligibility is not row authority. |
| `consumer/lookup` | `{domain,allocation_ref?,cursor?}` | Own `ParticipantDTO` page for the exact domain/optional target, including revoked tombstones. Empty for unknown or foreign allocations. No DNS probe or write. Protected key only. |
| `consumer/status` | `{client_ref}` | Own `ParticipantDTO` plus `alias:AliasDTO`, resolved before DNS lookup. Unknown/foreign refs are identical 404. Protected key only. |
| `consumer/join` | `{allocation_ref,client_ref,domain}` | Exact legacy `{client_ref,domain,source,target}` after verifying an existing active grant (or exact owner binding), stored zone and one-hop CNAME. No creation, adoption or allocate fallback. Protected key only. |
| `allocation/list` | `{domain,cursor?}` | Exact-domain `AllocationDTO` page, filtered by controller authority before pagination. A true hub superuser may resolve all matching existing allocations. |
| `trust/list` | `{allocation_ref,cursor?}` | `ControllerParticipantDTO` page including owner/revoked rows. Exact allocation controller only. |
| `trust/recipient` | `{allocation_ref,consumer_project_uuid}` | `{consumer_project_uuid,label,active:true}` for one effectively active group, after controller check. Label max200; no group directory or key-existence oracle. |
| `trust/grant` | `{connection_request}` | `ControllerParticipantDTO`. Controller, matching domain/target, exact one-hop alias and active recipient required. Exact active retry returns the same row. |
| `trust/revoke` | `{allocation_ref,participant_ref}` | Revoked `ControllerParticipantDTO` after durable revocation and union cleanup. Owner revocation is refused. No alias proof; provider failures return 503 while revocation persists. Retry/sweep repairs cleanup. |

`ParticipantDTO` has **exactly**:

```text
allocation_ref, participant_ref, client_ref, domain, source, target, role,
trust_state, scope, can_publish, can_withdraw, revoked_at
```

`participant_ref` is a UUID; `allocation_ref == target`,
`source == "_acme-challenge." + domain`, `role` is `owner|trusted`,
`trust_state` is `active|revoked`, `scope` is exactly `[domain,"*."+domain]`,
and `revoked_at` is an ISO timestamp or null. `can_publish` and `can_withdraw`
describe the current caller's authority, not provider/DNS success. An active
protected same-project key can withdraw after trust revocation; it cannot publish
under a revoked grant. `ControllerParticipantDTO` adds exactly
`consumer_project_uuid` and `consumer_label` (max200). Consumer DTOs reveal no
other consumer identity, lease refs or values.

`AllocationDTO` has exactly
`{allocation_ref,domain,source,target,owner_project_uuid,can_manage_trust:true}`.
`AliasDTO` has exactly
`{state,observed_target,one_hop,checked_at}`. State is `match|mismatch|lookup_failed`;
a matching safe single target has `one_hop:true`; target chaining has false;
missing/multiple source targets have null/null. Lookup failures have null/null
and expose no resolver raw error. `checked_at` is ISO. This is a CNAME observation,
not TXT/certificate convergence.

Pages have exactly `{items:[...],next_cursor:null|string}`, maximum20 ordered by
pk. Cursor is a positive decimal pk string; every exact domain/allocation and
authority filter applies before cursor and limit. Forging a cursor cannot widen
access.

### Trust handshake and custody

Cold preflight calls capabilities and own lookup before creating a local row.
An old hub's capabilities404 means `shared_validation_unsupported`, never
permission to allocate a new target for a used CNAME. Connect preparation
persists a fresh local ref and exports this exact nonsecret object:

```json
{"version":1,"consumer_project_uuid":"0123456789abcdef0123456789abcdef",
 "client_ref":"persisted-local-ref","domain":"customer.example",
 "allocation_ref":"existing-target.acme-hub.example.net"}
```

The controller recognizes the exact recipient and explicitly grants **apex and
wildcard certificate issuance authority**. The project rechecks and joins with
that same stored ref. Anyone obtaining that authority can hold an independent
certificate/private key for the domain, enabling impersonation; present that
scope before granting. Descriptor knowledge alone grants nothing. A consumer
lacking authority copies its request and awaits the controller; do not ask it to
mint privileged keys or edit database rows. Hub URL/key are existing file-only
server configuration and are never browser inputs or read back.

Revocation is permanent; a revoked ref never reactivates or rebinds. Fresh
regrant needs a new persisted local ref and participant namespace. Old retries
can affect only their old namespace. Equal challenge refs across projects are
isolated and Route53 receives the deduplicated live union. Revocation blocks new
accepted publish intent and removes only that participant's desired leases; it
does not revoke issued certificates or undo an already-accepted CA request.

Inactive/expired/revoked keys never authenticate, even for cleanup. An active
rotated protected key in the original group may withdraw revoked leases without
CNAME proof. Otherwise expiry (default900s, clamp60–86400s), the five-minute sweep
and provider availability bound cleanup. Ambiguous failures remain durable and
retryable; do not promise cleanup through a provider outage.

### Safe shared-operation errors

Normal failure envelopes carry these exact machine tokens; use product copy
for display and never persist arbitrary remote error prose:

| HTTP | Error |
|---|---|
| 400 | `acme_invalid_request` (shape/normalization/version) |
| 403 | `acme_not_authorized` (auth/controller denial, identical for unknown/foreign allocation) |
| 404 | `acme_not_authorized` (unknown/foreign consumer-local ref) |
| 403 | `acme_trust_revoked` (only after proving own revoked participant) |
| 409 | `acme_reference_conflict` (permanent conflicting/revoked binding; fresh ref required) |
| 400 | `acme_alias_mismatch` |
| 503 | `acme_alias_lookup_failed` |
| 503 | `acme_hub_unavailable` |

Legacy owner request/response keys, statuses and error behavior remain
compatible. A trusted revoked publish can return `acme_trust_revoked`.
Do not add participant metadata to legacy four/six-field replies or `/dnsman/config`.
A newly upgraded hub keeps sharing disabled until its one-time enable action in
the existing Portal. Capabilities returns the same v2 shape with
`shared_validation:false` and both capability booleans false. New setup, grant
and join operations return 503; exact existing participant status and its own
publish/withdraw remain available. Owner issuance and already-pinned published 1.38 trusted connections continue
normal renewal under their existing trust and revocation checks. New setup,
grants and joins stay unavailable until enabled. Missing installation state fails closed.

Deploy every expected node through the existing controls, then enable using the
[authenticated Admin hub endpoint](../account/admin_portal/fleet_configuration.md#enable-hub-sharing).
No separate operator login, root enrollment or attestation ceremony is required.
See [the rollout and rollback boundary](../../django_developer/dnsman/HubUpgrade.md).

## Project shared-validation setup

These **local project** endpoints connect to an existing allocation through the
installation's configured hub. The [hub v2 API](#shared-validation-v2) remains
the separate controller/consumer protocol. Setup does not create a target,
replace the public CNAME, change provider credentials, grant trust or mint a
protected federation key. Each project issues and renews its own certificate
and keeps its own private key.

A target is exactly `{domain:12}` or `{group:7,name:"customer.example"}`. The
group must already exist and be effectively active. An existing same-name
Domain must belong to that exact group; no house adoption or transfer is
implicit. Resolve local object authority before any hub read. Read requires
`view_dns`, `manage_dns` or `security` for the target; mutations require
`manage_dns` or `security`. Key-backed override sessions remain confined to the
underlying key's group and authority. A groupless house Domain is readable only
within its existing authority and ineligible for shared mutations: no request
is exported, mutation capabilities are false, and `next_action` is
`{code:"unsupported_domain_scope",actor:"deployment_operator"}`. Shared setup
currently requires a group-managed domain; existing direct-provider operation
remains available.

| Local route | Exact input | Effect |
|---|---|---|
| `GET delegation/plan` | Target, optional `allocation_ref` | Read-only Readiness v1, also used for polling status. No row/ref/account/job creation or stored demotion on a failed observation. |
| `POST delegation/connect` | Target, `allocation_ref`, optional `restart:false` | Preflight, prepare/export a durable request, or replay join after controller grant. |
| `POST delegation/check` | `{delegation:9}` | Persist verified/broken result and bind the local Domain after current grant/one-hop proof. |
| `POST delegation/request` | `{delegation:9}` | Idempotent request for the fixed exact apex-plus-wildcard profile. No names, force or key input. |
| `GET delegation/group-choice` | `id` OR `search/start/size` | Minimal authorized active local group choices, default25/max50. Recheck the exact action after selection. |

All routes are under `/api/dnsman`. Group choices derive from setup authority,
including inherited active memberships; they are not the global-only
`credential/group-choice` assignment surface. Group selection cannot confer
hub consumer or controller authority. Inputs contain no hub URL, token, key or
caller-supplied consumer UUID.

### Prepare, export, grant, replay

Examples omit authentication and use fake public refs.

```http
GET /api/dnsman/delegation/plan?domain=12&allocation_ref=existing-target.acme-hub.example.net
```

A first Plan has no connection/client ref and cannot export a stable request.
It performs read-only capabilities and own-participant discovery. Old-hub
capabilities404 means unsupported; show `upgrade_hub`. It never allocates a
replacement or changes local routing after an unsuccessful preflight.

```http
POST /api/dnsman/delegation/connect
Content-Type: application/json

{"domain":12,"allocation_ref":"existing-target.acme-hub.example.net"}
```

Successful preparation pins the server-authenticated consumer identity and
fresh immutable client ref before any ambiguous join call. Missing trust
returns `grant_trust` and this **exact five-field public request** in
`connection_request`:

```json
{
  "version": 1,
  "consumer_project_uuid": "0123456789abcdef0123456789abcdef",
  "client_ref": "11111111-2222-4333-8444-555555555555",
  "domain": "customer.example",
  "allocation_ref": "existing-target.acme-hub.example.net"
}
```

Present scope separately as `["customer.example","*.customer.example"]`.
The hub consumer UUID is the configured protected key's original project,
independent of the local Domain tenant. The descriptor has no authority by
itself and is safe to hand to the controller. Do not add credentials to it.

The controller recognizes the recipient and explicitly grants apex **and**
wildcard issuance authority through the hub:

```http
POST /api/dnsman/acme/trust/grant
Content-Type: application/json

{"connection_request":{"version":1,"consumer_project_uuid":"0123456789abcdef0123456789abcdef","client_ref":"11111111-2222-4333-8444-555555555555","domain":"customer.example","allocation_ref":"existing-target.acme-hub.example.net"}}
```

That is a controller action under the hub's stronger authority; ordinary local
`manage_dns` cannot grant it. Then replay the original **local** Connect request.
It joins using the same stored ref, consumer, allocation, domain and scope.
A lost response is retried without generating another ref. A changed key project
reports `identity_changed` and cannot silently repin the connection.

```http
POST /api/dnsman/delegation/check
Content-Type: application/json

{"delegation":9}
```

Check revalidates local ownership, active grant and exact one-hop alias before
persisting proof. Existing Domain provider, tenant, credential, hosted-zone and
certificate/vhost references stay intact. A new external name becomes a
certificate-only `mojo` Domain only after proof succeeds.

```http
POST /api/dnsman/delegation/request
Content-Type: application/json

{"delegation":9}
```

An exact valid not-due certificate returns `already_current`; a real
pending/running job or active issuer returns `already_queued`; due active
material renews the same certificate id. Pending/issuing rows with terminal or
missing jobs return `job_held` unchanged and direct inspection. Request rechecks the current
generation under the Domain lock; a concurrent restart returns
`stale_connection`. If an active row's material cannot be read, Request returns
`custody_unavailable` without queueing and readiness directs the operator to
restore custody.
A conflicting SAN profile is refused rather than changed or downgraded to an
exact-host certificate. Poll Plan for status; the request does not claim that
an edge host already installed the new material.

For a revoked binding or an unjoined pending request, explicit recovery uses
Connect with the same target/allocation and `"restart":true`. The old generation
is retired and its cleanup must be settled; a fresh immutable ref is exported
and requires a fresh controller grant. Restart cannot swap an active grant,
revive a revoked ref or repoint the allocation. Valid certificate material,
renewal dates and vhost references survive setup and unsuccessful renewal.

### Readiness v1

Each setup action returns the same data object in the normal response envelope.
All top-level fields are present:

```text
version: 1
checked_at: ISO timestamp
domain: {id, name, provider, group: {id, name}|null}
profile: {name: "apex_wildcard", names: [apex, "*."+apex]}
hub: {state, protocol_version, consumer_project_uuid}
connection: {id, mode, state, client_ref, allocation_ref, participant_ref, verified_at}|null
trust: {state, can_publish, can_withdraw}
alias: {state, source, target, observed_target, one_hop, checked_at}
certificate: {id, state, names, not_after, renew_after, serving_valid, material_state}|null
renewal: {state, next_attempt_at, attempts, error_code, cleanup_pending,
          job: {id, state}|null, worker_version}
capabilities: {group_choices, can_connect, can_restart, can_check, can_request}
next_action: {code, actor}
connection_request: {version, consumer_project_uuid, client_ref, domain, allocation_ref}|null
operation: {action, outcome, changed}
```

An external name can have `domain.id:null` before proof. Connection mode is
`allocated|shared`; persisted states remain `pending|verified|broken|retired`.
View-only callers receive no descriptor and false mutation capabilities.
`operation` describes the chosen action, not an automatically chained workflow.

| Field | States / interpretation |
|---|---|
| `hub.state` | `unconfigured`, `misconfigured`, `unavailable`, `unauthorized`, `unsupported`, `ready` |
| `trust.state` | `unknown`, `not_requested`, `missing`, `active`, `revoked`, `identity_changed` |
| `alias.state` | `unknown`, `match`, `mismatch`, `lookup_failed`; a chained alias has `one_hop:false` |
| `certificate.material_state` | `available`, `missing`, `unavailable`; `serving_valid` is nullable when custody cannot be established |
| `renewal.state` | `not_requested`, `scheduled`, `queued`, `issuing`, `retrying`, `blocked`, `failed`, `expired`, `unknown` |

Serving validity and renewal readiness are separate. A retrying/blocked renewal
may retain a still-valid certificate. The material check establishes API-node
custody for only the selected row, returns no PEM/key, and proves no edge-host
installation. Renewal exposes only that certificate's summarized job id/state,
attempts, next attempt, allowlisted error and cleanup flag; never raw job or
remote error payloads. `worker_version` remains unknown without established
worker telemetry. An API version cannot prove an old long-lived worker restarted.

`next_action` is the first unmet prerequisite, with one of these fixed codes:
`configure_hub`, `upgrade_hub`, `repair_hub_access`, `retry_hub`,
`choose_allocation`, `prepare_connection`, `grant_trust`, `restart_connection`,
`connect`, `repair_alias`, `check`, `request`, `wait_for_job`, `repair_worker`,
`restore_custody`, `inspect_failure`, `unsupported_domain_scope`, `none`.
Actor is `deployment_operator`, `allocation_controller`, `domain_owner`,
`project_operator` or `none`. Render product copy for the code and actor;
never execute a command supplied by a remote response.

### Errors and client recovery

GET Plan reports readiness/prerequisite failures as data with HTTP200. Mutation
prerequisite failures return the normal false envelope with HTTP409, an
allowlisted `error_code` and Readiness v1 in `data`:

`trust_missing`, `trust_revoked`, `hub_identity_changed`, `allocation_conflict`,
`alias_mismatch`, `alias_lookup_failed`, `connection_not_verified`,
`certificate_busy`, `cleanup_pending`, `profile_conflict`, `stale_connection`,
`unsupported_domain_scope`.

Invalid shape is400, target permission is403, inaccessible ids are404,
configuration/unsupported/unavailable is503, and rate limit is429. Keep the
readiness object available on an action error so the UI can show the next
repair. Display fixed copy instead of arbitrary hub exception text. A busy
certificate means an existing worker owns its issue lock: retry later without
preparing a different request. A failed plan observation does not persist a
verified-to-broken demotion; explicit Check and runtime issuance validation may.

Shared pending/broken/revoked/identity-changed connections block certificate
issuance before CA/provider writes. Formerly verified retired tombstones retain
the guard until a successor verifies. Legacy allocated pending rows remain
inert. No shared failure silently writes TXT through Route53/GoDaddy beside the
CNAME or downgrades the requested profile. Trust revocation stops future
validation and does not revoke an already-issued certificate.

### Deployment and rollback

Hub URL/key remain server-only static deployment settings, delivered through
the existing deployment configuration and never accepted/read back through
these endpoints, CLI output or remote-agent input. Restart API and worker
processes after their configuration/binary upgrade. Deploy the hub compatibility release first and complete its separately authorized
[operator cutover](../../django_developer/dnsman/HubUpgrade.md). Apply the
additive downstream schema, then drain/restart **all** old issuance
workers before enabling Connect; old workers do not understand shared pending
guards. Keep legacy rows in allocated mode. Existing direct Domain/provider and
certificate rows are preserved in place. Verify a stubbed scheduled renewal
through the existing job, KMS and edge handoff before separately authorized
production adoption.

Before any shared connection, application rollback can retain the additive
schema. After shared rows exist, retain the new binaries/guards and valid
material; repair configuration, alias and trust or explicitly restart an
eligible connection. Do not delete/retire a row to regain direct renewal or
downgrade workers. A return to direct DNS is a separately authorized handover.
This feature does not authorize live production changes or lift a hold.

### Temporary certificate ownership and queued workers

Setup releases temporary issue advisory locks while the Domain transaction is
still held, before commit callbacks expose a new job. A worker that encounters
any temporary owner reports the fixed `dnsman_certificate_busy` outcome instead
of completing a skipped renewal. Its existing Job receives a durable Jobs
retry budget of 8, including jobs originally configured with zero retries
(at most eight busy attempts).
Only that lock collision is retryable at the Jobs boundary: CA/validation
failures retain certificate backoff and valid serving material.

If busy retries exhaust (or expire while busy), the existing certificate cron
can recover due renewals and initial pending issuance through the existing Jobs
replacement-retry service. Root/leaf locks serialize one exact replacement;
consumer profile, function, certificate id and channel stay fixed. Recovery
follows the actual replacement leaf and stops for cancellation, completion or a
different failure. It does not revive an operator-canceled job. Readiness keeps
serving validity separate, reports an exhausted busy worker as failed and points
to `repair_worker`. Request returns `job_held` without changing a preserved
terminal job (including completion), or a pending/issuing row without a Job.
Only a real queued/running job or observed active issuer justifies
`already_queued`; held work directs inspection. No extra scheduler or installer
is introduced.

## Protected recipient setup and rotation

The deployment operator uses the [private shared-validation Fleet topic](../account/admin_portal/fleet_configuration.md#shared-validation-topic)
to select an installed approved hub, preview a write-only key, publish it, Apply,
and observe all expected nodes. Tenant connection status must use its safe
readiness/repair codes and operator handoff; never embed this private profile
catalog or credential input in a tenant flow. Configuration health and actual
shared-validation activation are separate. No operation here grants controller
allocation trust.

Connect/restart can return `configuration_busy` with HTTP 409 when another setup
or configuration operation owns identity admission. No connection reference is
created, retired or rotated, and no hub mutation is attempted by that refusal.
Show a busy message, Refresh the current plan, and require a deliberate retry;
do not automatically replay Connect/restart. Refreshed identity changes can still
require the existing `hub_identity_changed` repair. Protected preview, publish and
historical restore use the same bounded busy response; the private operator flow
must Refresh configuration and preview again before an explicit publication.
Busy refusal also preserves every connection diagnostic, including a newly
observed revocation. A later admitted restart retains that observation on its
retired predecessor; it cannot authorize a different pending generation.

On the hub, `GET /api/account/admin/fleet/shared-validation/recipient` accepts only
`consumer_project_uuid=<exact original hub Group UUID>`. Its response is
`{purpose:"acme_validation_consumer",consumer_project_uuid,consumer_label,
permission:"dnsman_acme_federation",can_issue,grant_explanation}`. Show the real
recipient label and exact UUID for confirmation. The route does not enumerate
projects or infer the hub UUID from a downstream tenant.

Issue with exactly `{consumer_project_uuid,expected_consumer_label,label,
purpose:"acme_validation_consumer"}` by POST to the same route. Writes require
an interactive active literal superuser, same origin and fresh auth. The backend
re-resolves the original effectively active Group and checks protected grants
under lock. A changed label returns 409 and requires reload. Unknown/inactive
recipients return the same bounded unavailable response. Success returns
`{purpose,consumer_project_uuid,consumer_label,api_key:{id,name,is_active,permission},token}`
through the existing non-cacheable secret-response pattern. No arbitrary
permissions, linked user, override mode or raw key-create payload is accepted.
Keep tokens transient in UI state; configuration reads, previews, histories and
receipts never return them. Existing explicit audited reveal remains supported;
do not promise a one-time unrecoverable token.

Recommend staged rotation: issue replacement → preview/publish → Apply → verify
every expected node → explicitly deactivate the old hub key using
`/api/account/admin/apikey/action`. Existing in-place rotate invalidates its prior
token immediately; disclose that before invoking it. Revoke/deactivate/reveal
remain existing explicit key operations. After uncertain issuance or publication,
refetch authoritative key inventory/configuration before retrying. Missing catalog,
delegation, publisher or node enrollment must show the supported Setup/AWS/Capacity
operator destination and application-adoption responsibility, without asking the
operator to edit local files or rerun initial bootstrap.
