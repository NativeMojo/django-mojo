# Email Setup Audit (SES) — what exists, what was broken, what dnsman supersedes

Audit performed 2026-07-26 as part of the dnsman build (maestro item 392, stage 5).
Line numbers are from that commit.

The short version: SES onboarding in this framework has always **computed** the DNS
records correctly and then left applying them to whoever called it. That gap produced
three real problems — a `dns_mode="route53"` setting that did nothing, GoDaddy API
secrets travelling in request bodies, and an AWS access key id served unmasked in a
REST graph. dnsman closes all three.

---

## 1. The pieces, as they stood

### `mojo/helpers/aws/ses_domain.py` — the SES orchestration helper

Model-free, 1100+ lines. The parts that matter here:

| Function | Line | What it does |
|---|---|---|
| `build_required_dns_records` | 134 | Computes the record set SES needs: `_amazonses.<domain>` verification TXT, one `<token>._domainkey.<domain>` DKIM CNAME per token, and — when MAIL FROM is enabled — an MX (`10 feedback-smtp.<region>.amazonses.com`) plus an SPF TXT on the MAIL FROM subdomain. |
| `onboard_domain` | 1004 | The "one-step" orchestrator: requests verification + DKIM, calls `build_required_dns_records`, reconciles SNS topics / notification mappings / receiving. **Applies no DNS.** Its own docstring says so: it returns `dns_records` and expects the caller to apply them. |
| `apply_dns_records_godaddy` | 1074 | The only apply path that existed. Takes `api_key` / `api_secret` as arguments, builds a `DNSManager`, and PUTs each record. |
| `apply_dns_records_route53` | 1108 | **New in this item** — the twin. Resolves the hosted zone (`route53.find_zone_id`) when not given, quotes TXT/SPF values via `route53.format_txt_value`, passes MX priority strings through verbatim, and collapses records that share a `(type, name)` pair into one UPSERT (an UPSERT replaces the whole record set, so writing them one at a time would keep only the last value). |

The `DnsMode` type alias at line 44 has always been `Literal["manual", "route53", "godaddy"]`.

### `mojo/apps/aws/services/email_ops.py` — the service layer

`onboard_email_domain` (line 228) resolves configuration off the `EmailDomain` row,
calls `ses_domain.onboard_domain`, then — at lines 321–330 — applies DNS **only** for
GoDaddy, and only when `godaddy_key` and `godaddy_secret` were passed in:

```python
if dns_mode == "godaddy" and godaddy_key and godaddy_secret:
    apply_dns_records_godaddy(...)
elif dns_mode == "godaddy":
    result.notes.append("DNS mode is GoDaddy but credentials not provided; ...")
```

### `mojo/apps/aws/rest/email_ops.py` — the endpoints

Three endpoints, all gated `@md.requires_global_perms("manage_aws", "comms")`:

- `email/domain/<int:pk>/onboard` (line 85)
- `email/domain/<int:pk>/audit` (line 158)
- `email/domain/<int:pk>/reconcile` (line 206)

The onboard endpoint forwards `godaddy_key` / `godaddy_secret` straight from the
request body into the service.

### `mojo/apps/aws/models/email_domain.py` — the model

`EmailDomain(MojoSecrets, MojoModel)` stores `name`, `region`, `status`,
`receiving_enabled`, the S3 inbound bucket/prefix, the four SNS topic ARNs, and
`dns_mode` (line 59). AWS credentials live in `MojoSecrets` and are read through the
`aws_key` / `aws_secret` properties.

---

## 2. Findings

### Finding 1 — `dns_mode="route53"` was a stub that was never implemented

`dns_mode` accepts `manual | route53 | godaddy` (model line 59, migration
`mojo/apps/aws/migrations/0001_initial.py:47`, and the `DnsMode` alias at
`ses_domain.py:44`). Nothing anywhere acted on the `route53` value:

- `ses_domain.onboard_domain` takes `dns_mode` as a parameter and **never reads it**
  (lines 1004–1070) — it applies no DNS in any mode.
- `services/email_ops.onboard_email_domain` branches on `dns_mode` exactly once, for
  `"godaddy"` (lines 321–330). A domain set to `route53` fell through both branches and
  silently got the `manual` behavior: records returned in the response body, nothing
  written to the zone.
- There was no `mojo/helpers/aws/route53.py` at all before this item, so there was
  nothing for such a branch to call.

Net effect: setting `dns_mode="route53"` looked like automation and did nothing. The
failure was silent — the response still carried `dns_records`, so callers who did not
also check their zone would conclude onboarding had succeeded.

**Fixed by**: `apply_dns_records_route53` (`ses_domain.py:1108`) plus
`mojo/apps/dnsman/services/email.py`, which applies the computed records through the
provider dispatch. `onboard_email_domain` sets `EmailDomain.dns_mode` to the provider
that actually received the records, so the field now describes reality.

### Finding 2 — provider API secrets travelled in request bodies

The only automated apply path required the caller to POST `godaddy_key` and
`godaddy_secret` to `email/domain/<pk>/onboard`. That put a **long-lived, account-wide
GoDaddy credential** into request bodies, proxy logs, browser devtools, and any client
that stored the form. It also meant the credential had no home: it could not be rotated,
audited, or revoked, because the system never held it.

**Fixed by**: the optional `use_dnsman` flag on the onboard endpoint
(`rest/email_ops.py:39` `_onboard_via_dnsman`, dispatched at line 108). With
`use_dnsman: true` the credential is read from the `DnsCredential` linked to the dnsman
`Domain` — a row with masked graphs, an `is_active` flag, a `verified` flag, and
rotation support. Nothing provider-specific is sent by the client.

`godaddy_key` / `godaddy_secret` still work and are **deprecated**, not removed —
downstream consumers keep working through this release. New integrations must use
`use_dnsman`.

### Finding 3 — `EmailDomain`'s default graph leaked `aws_key` unmasked

The `default` graph's `extra` list read:

```python
"extra": [
    "aws_key",          # ← raw AWS access key id
    "aws_secret_masked"
]
```

Every caller who could read an email domain (`VIEW_PERMS = ["manage_aws", "comms"]`)
received the full AWS access key id in the response body — in list responses too. The
secret half was masked, which shows the masking was intended and the key half was an
oversight. An access key id is not a password, but it is half of a credential pair, it
identifies the IAM principal, and it is exactly what a compromised secret needs to be
usable. Logged in maestro's `docs/AttentionNeeded.md`.

**Fixed by**: a new `aws_key_masked` property (`email_domain.py:164`) mirroring
`aws_secret_masked`, and swapping the graph entry (`email_domain.py:147`). The raw
`aws_key` property stays for internal callers (`services/email_ops._get_aws_credentials`
at line 124 uses it) but appears in **no** graph. Regression-tested in
`tests/test_dnsman/7_email.py::test_email_domain_default_graph_masks_the_aws_key`, which
asserts both the graph declaration and the serialized output.

### Finding 4 — GoDaddy record names are rewritten with a substring replace

`apply_dns_records_godaddy` (line 1096) converts an FQDN to GoDaddy's relative label with

```python
name = r.name.replace(f".{domain}", "")
```

A `str.replace` removes *every* occurrence, not just the suffix. For SES records the
labels are `_amazonses`, `<token>._domainkey` and `feedback`, none of which can contain
the domain, so this is safe **for the SES record set** — which is the only thing this
function is called with. It is not safe as a general FQDN→label helper. Left as-is
deliberately: changing it would alter behavior for existing callers, and dnsman's own
record CRUD does its own normalization (`mojo/apps/dnsman/services/naming.py`) rather
than reusing this line.

### Finding 5 — no propagation or verification step

Neither apply path waits for or checks propagation, and neither re-audits afterwards.
`audit_email_domain` exists and reports drift, but nothing calls it after an apply. Out
of scope for this item; noted so it is not mistaken for a regression.

---

## 3. What dnsman supersedes

| Before | Now |
|---|---|
| Caller decides the provider and passes matching credentials | `dnsman.services.email.apply_records(domain_obj, records)` picks the provider from `Domain.provider` |
| `dns_mode="route53"` silently did nothing | Route 53 records are applied through `apply_dns_records_route53` |
| GoDaddy key/secret in the request body | Read from the linked `DnsCredential`; fail-closed when it is missing, inactive, or unverified |
| Raw `aws_key` in the default graph | `aws_key_masked` |
| No record of which zone a domain lives in | The dnsman `Domain` row: provider, hosted zone id, credential, status |

### The new one-call path

```python
from mojo.apps.dnsman.services import email as dnsman_email

result = dnsman_email.onboard_email_domain(email_domain)   # instance or pk
```

It: resolves the `EmailDomain`; finds the dnsman `Domain` by name and refuses with a
404-status `ValueException` when the domain is not held here (or is not `active`); calls
`ses_domain.onboard_domain(..., dns_mode="manual")` to **compute** the records and
reconcile the SES/SNS side; applies them through `apply_records`; updates
`EmailDomain.dns_mode` to the provider that received them; and returns the SES result
plus an `applied` block naming the provider and the records written.

Over REST, the same thing:

```http
POST /api/aws/email/domain/<pk>/onboard
{"use_dnsman": true}
```

### Compatibility

- No model fields changed; no migration.
- `apply_dns_records_godaddy` is untouched — same signature, same behavior.
- `onboard_domain`, `audit_domain_config`, `reconcile_domain_config` are untouched.
- `services/email_ops.onboard_email_domain` is untouched, including the
  `godaddy_key`/`godaddy_secret` parameters.
- The only visible behavior change for an existing consumer is the graph fix: the
  `default` graph of `EmailDomain` now returns `aws_key_masked` instead of `aws_key`.

### Prerequisites for `use_dnsman`

1. A dnsman `Domain` row whose `name` matches the `EmailDomain` name, with
   `status="active"`.
2. For `provider="godaddy"`: an active, verified `DnsCredential` linked to that Domain.
   For `provider="route53"`: a `hosted_zone_id` (or a resolvable hosted zone) — the
   house AWS credentials are used.

### Still open (not addressed here)

- No post-apply propagation wait or re-audit (Finding 5).
- `EmailDomain` has no link to the dnsman `Domain`; they are matched by name. Adding an
  FK would need a migration and was deliberately kept out of this stage.
- The legacy `godaddy_key`/`godaddy_secret` parameters remain live and should be removed
  in a future release once consumers have migrated.
