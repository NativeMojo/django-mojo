# edge — vhost, route, upstream and blocklist API

How a domain gets served. Pairs with [dnsman](../dnsman/README.md), which owns
the domain and issues the certificate.

Backend reference: [django_developer/edge](../../django_developer/edge/README.md).

> **New here? Start with the walkthrough.**
> [Put your web app online](deploy_your_webapp.md) takes a person who knows only
> the web address they want from that address to a live, HTTPS-secured app, and
> then shows the day-2 management screen. This page is the endpoint reference
> behind it.

> **Serving one web app? You probably want the app-scoped endpoints instead.**
> `GET/POST /api/edge/webapp/serving`, `POST /api/edge/webapp/certificate`,
> `POST /api/edge/webapp/add_route` and `POST /api/edge/webapp/remove_route`
> express the same changes in terms of **the app** rather than the rows below,
> and — crucially — apply each one to the app's own address *and* every extra
> address it answers on, in one transaction. Editing the vhost and route rows
> here by hand does not: a pool moved on the primary alone leaves a custom
> domain on a node fleet that never installs that app's build. See
> [Put your web app online → Serving](deploy_your_webapp.md#managing-your-app-afterward)
> and the [backend reference](../../django_developer/edge/webapps.md#day-2-management).

Deploying site builds from GitHub (register, verify, converge, roll back):
[releases.md](releases.md).

Deploying API code to the fleet (webhook + manual trigger):
[deploy.md](deploy.md).

## The shape to expect

You never send nginx configuration. A vhost is a small set of choices, and the
API derives everything that ends up in a config file:

- **`server_name`** comes from the `domain` you name plus a `label` — you
  cannot type it, and you cannot claim a name under a domain your group does
  not own.
- **The web root** comes from the vhost's own id.
- **A proxy destination** is a reference to a declared `upstream` — for the
  whole host on an `api` vhost, per path prefix through **routes** on a
  `site_api` vhost. Never a URL you supply.
- **A redirect target** is a bare hostname, validated like a server name —
  never a URL.

A request carrying anything nginx would treat as syntax is rejected, not
escaped.

The deployment chooses its public listener posture, not each vhost. A
DNSMAN-only installation may serve these shapes on HTTPS alone; an installation
that deliberately supports HTTP redirects or HTTP-01 may also expose port 80.
That posture is file-managed and is not writable through the vhost API.

> **Kind enum change (2026-08).** `static`, `spa` and `proxy` are gone.
> Existing rows were migrated automatically: `proxy` → `api`, `spa` →
> `site` with `spa: true`, `static` → `site`. Consumers that send or branch
> on the old strings must update: sending `kind: "static"` is now a 400.
> The SPA/static distinction is the `spa` boolean on a `site` vhost, no
> longer a kind.

## Vhosts

```
GET    /api/edge/vhost                list (scoped to your group)
GET    /api/edge/vhost/<id>           detail
POST   /api/edge/vhost                create
POST   /api/edge/vhost/<id>           update
DELETE /api/edge/vhost/<id>           delete
```

**Permissions:** `view_dns` to read; `manage_dns` (or `security`) to write.

### Fields

| Field | Writable | Notes |
|---|---|---|
| `domain` | on create only | Owns the name and the tenancy. Immutable afterwards — changing it would move the vhost between groups. |
| `label` | yes | `""` serves the apex, `"*"` the wildcard, otherwise one DNS label (`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`). |
| `kind` | yes | `api`, `site`, `site_api`, or `redirect`. |
| `upstream` | yes | **Required** for `api`, **rejected** otherwise (`site_api` proxies per-route). |
| `certificate` | yes | Must belong to the same domain and cover the derived name. |
| `pool` | yes | Which fleet pool serves it. Default `"default"`. |
| `spa` | yes | `site`/`site_api` only: history-fallback to `index.html` instead of a 404. |
| `body_size_mb` | yes | Upload cap in MB, 1–4096, default 50. Always applied. |
| `quiet_paths` | yes | `api`/`site_api` only: exact request paths kept out of the main access log (health checks). Charset `[A-Za-z0-9._/-]`, must start `/`. On `site_api` each must sit under a declared route prefix. |
| `serve_static` | yes | `api`/`site_api` only: serve the platform's Django static files at `/static/` instead of proxying it. On `site_api`, nginx renders this as a `^~` prefix so the site's asset-suffix matching cannot capture it. |
| `redirect_to` | yes | `redirect` only (**required** there): the target hostname. A 301 preserving the request path is rendered. |
| `is_enabled` | yes | Only enabled vhosts are served. |

`server_name` is returned on every graph as a read-only extra.

### Errors worth handling explicitly

| Situation | Response |
|---|---|
| Two **enabled** vhosts on the same `domain` + `label` | 400 — disable the old one first, or stage the replacement with `is_enabled: false` |
| `certificate` belongs to another domain, or does not cover the name | 400 |
| `api` with no `upstream`; `site`/`site_api`/`redirect` with one | 400 |
| `redirect` with no `redirect_to`; any other kind with one | 400 |
| A knob on a kind it does not apply to (`spa` on `api`, `quiet_paths` on `site`, …) | 400 |
| A `site_api` quiet path not under any declared route prefix | 400 — the message names the declared prefixes |
| `label` containing a dot, uppercase, or any punctuation | 400 |

A vhost may be created **disabled** with a certificate that does not yet cover
it — useful while a certificate is being reissued. The coverage check applies
at enable time.

## Routes (`site_api` prefixes)

```
GET    /api/edge/route                list (scoped to your group)
GET    /api/edge/route/<id>           detail
POST   /api/edge/route                create   {vhost, path_prefix, upstream}
POST   /api/edge/route/<id>           update
DELETE /api/edge/route/<id>           delete
```

**Permissions:** same as vhosts (`view_dns` / `manage_dns` / `security`),
resolved through the owning vhost's domain.

- `vhost` must be a `site_api` vhost in your group.
- `path_prefix` starts with `/`, same charset as quiet paths; a bare `/` is
  rejected (that is what `kind: api` is for). Longest prefix wins at
  request time, so `/api` and `/api/ws` can coexist pointing at different
  upstreams. Each route renders as an nginx `^~` prefix, taking precedence
  over the site's asset-cache matching so asset-shaped paths such as
  `/api/app.js` still reach the route.
- `upstream` must be a shared (house) upstream or one belonging to your
  group — another tenant's upstream is a 400.
- Deleting an upstream that routes still reference is refused; retire the
  routes first.

### WebApp hosted-auth routes

WebApps created by the onboarding wizard are `site_api` sites with their
same-origin login surface installed automatically. `/auth`, `/register`,
`/passkey`, `/api/auth`, `/api/account`, `/api/login`, and
`/api/refresh_token` go to the declared Django auth upstream. Your ordinary
application API client should continue to use its configured API origin.

The legacy bouncer honeypots `/login`, `/signin`, and `/signup` are also sent
to Django, but only on an **exact** path match. Nested application pages such
as `/signin/login` and `/signin/callback` therefore remain WebApp/SPA routes.
Do not add a `/signin` prefix route manually.

For command-line bootstrap, pass the platform-declared upstream id with
`webapp_bootstrap --auth-upstream <id>`. Existing hosted apps are repaired
automatically when the Edge job engine starts after a django-mojo deployment.
The operator fallback `webapp_bootstrap --webapp <id> --auth-upstream <id>
--routes-only` performs that repair on one app without touching its deployment
key. Existing hosted-auth routes are reconciled idempotently; a conflicting
destination is refused instead of being silently repointed.

## Upstreams

```
GET  /api/edge/upstream               list the ones you may select
GET  /api/edge/upstream/<id>          detail
POST /api/edge/upstream/<id>          update  (only `is_enabled` is writable)
POST /api/edge/upstream/declare       create   — PLATFORM ADMIN ONLY
POST /api/edge/upstream/retire        disable  — PLATFORM ADMIN ONLY
```

**You will normally only call the read endpoints.** Declaring an upstream is
restricted to platform administrators, because this row is the allowlist that
makes a proxying vhost safe — if any tenant could add one, the reference would
stop being a constraint. Build vhost and route forms as a **select** over
`GET /api/edge/upstream`, which returns your group's upstreams plus the
platform's shared ones.

`host`, `port`, `socket_path` and `kind` are not writable over REST at all,
including for platform admins — an existing upstream cannot be repointed, only
retired and replaced. A retired upstream stops its vhosts being served (they
are excluded from the fleet's desired state, with an incident) rather than
silently repointing traffic.

## Blocklist

```
GET    /api/edge/blocklist            list
GET    /api/edge/blocklist/<id>       detail
POST   /api/edge/blocklist            create   {kind, value, mode, note}
POST   /api/edge/blocklist/<id>       update
DELETE /api/edge/blocklist/<id>       delete
```

**Permissions: GLOBAL security grants only** — `view_security` to read,
`manage_security` (or `security`) to write. The blocklist is fleet-wide (one
edge protects every tenant behind it), so there is no group scoping and a
`?group=` param opens nothing: a group-member grant is not enough.

| Field | Values | Notes |
|---|---|---|
| `kind` | `ip`, `ua` | IP/CIDR, or a user-agent regex fragment. |
| `value` | — | `ip`: stored normalized (`10.1.2.3/8` → `10.0.0.0/8`). `ua`: matched case-insensitively; letters, digits and `()[]|?^.*+-/_\` only — no spaces, quotes, braces or `$`. |
| `mode` | `allow`, `off`, `log`, `enforce` | `log` (default) observes in the edge watch log; `enforce` blocks with 444; `allow` exempts a client from BOTH; `off` parks the rule. |
| `note` | free text | Why the rule exists. |

**Log-first:** create rules in `log`, watch the edge watch log (each line
names the matching rule's id), then flip to `enforce`. Changes converge to
the fleet within the normal convergence window (~10 minutes).

## Machine endpoints

```
GET /api/edge/desired_state?pool=default
GET /api/edge/material/<certificate-id>
```

These are for **serving nodes**, not for browser clients. They require the
global `edge_node` permission, which is protected: a group administrator cannot
grant it to an API key, and a member-scoped grant plus `?group=` does not open
them. Nothing in a portal should call these.

Each `desired_state` vhost carries non-secret certificate identity and revision
metadata (`certificate` and `certificate_serial`). Changing the serial during
an in-place renewal changes the desired-state generation, so nodes follow the
ordinary staged install path, fetch the new material through `material`,
validate it, and reload nginx. `desired_state` itself never carries certificate
PEM, chain, or private-key material.

`GET /api/edge/proof?pools=default,blue` is another node-only endpoint with the
same protected global `edge_node` permission (user or provisioned machine API
key). `pools` may be a comma-separated query value or list; omitting it reads
only `default`. The node must have a valid file-only `EDGE_NODE_ID`, and every
requested pool must satisfy the configured pool-name grammar. Invalid input or
node configuration is refused rather than returning partial proof.

```json
{
  "node_id": "edge-a",
  "django_mojo_version": "1.9.0",
  "pools": {
    "default": {
      "generation": "sha256-generation",
      "excluded": 0,
      "www_pending": 0,
      "cert_pending": 0,
      "serving_generation": "sha256-combined-generation",
      "current_generation": "sha256-combined-generation"
    }
  }
}
```

It returns no paths, settings, PEM, deployment token, or credential. Browser
portals must consume the literal-superuser-only System Setup readiness report
instead of calling node proof directly.

`generation` proves that pool's desired configuration. On a multi-pool node,
`serving_generation` proves the atomic union installed for all assigned pools,
and `current_generation` proves what nginx's global `current` link names now.
Readiness requires the latter two to match for every pool; a UI must not infer
green from the per-pool generation alone.

Vhost and Route mutations publish convergence after commit. Route editors may
preserve successful rows when a later row fails, display that partial result,
and retry only the failed rows; each committed desired-state generation is
idempotently published and readiness remains pending until every expected
node/pool proves it.

Although each publication receipt names the affected pool and generation, a
node handles it by atomically installing the union of all pools assigned to
that node. Portals should treat the receipt as convergence-pending evidence,
not as proof that one pool was independently swapped live.

## WebApp onboarding and day-2 API

The endpoints a browser console (the built-in Admin portal, or your own) calls
to stand up a site through the **URL-first wizard** and manage it afterward. For
the friendly, zero-knowledge walkthrough see
[Put your web app online](deploy_your_webapp.md); for the CI deploy contract
(register/verify/complete, rollback, key rotation) see [releases.md](releases.md);
the backend model reference is
[django_developer/edge/webapps.md](../../django_developer/edge/webapps.md).

**"Human-only" means an interactive user session** — a real browser login. API
keys, group tokens, and override-user key sessions are refused, so automation
cannot drive onboarding or start a deployment out of band. Endpoints marked
**fresh-auth** additionally require an interactive login completed within the
last few minutes (600 seconds for onboarding and key mint/rotate; 300 for key
revoke).

Onboarding authority is `security`, or both `manage_webapp` and `manage_dns`,
resolved globally or through the selected group (exact or inherited member
grant); creating the owning group additionally needs global
`manage_groups`/`groups`. Every onboarding call is actor- and origin-bound, and
an operation's detail is readable only by the actor who created it.

### Onboarding wizard

| Method | Endpoint | Purpose | Access |
|---|---|---|---|
| GET | `/api/edge/webapp/onboarding/precheck?url=<address>` | URL-first pre-flight. Normalizes the typed address and returns a `verdict` before any operation is created: `ready`, `records_needed`, `apex`, `deep_label`, `path`, `taken`, `conflict`, `domain_unknown`, `configuration_required` (this installation cannot serve app addresses yet), or `invalid`. A managed-domain `ready` carries a `destination` object, **not** `records` — the platform writes that record itself (and writes nothing when a `*.{domain}` wildcard already points at the destination). A wildcard-synthesized DNS answer never produces `conflict`; only a genuine host-specific foreign record does. Domain matching (`ready`/`conflict`/`taken` vs. `domain_unknown`) is **ancestor-aware**: an address under a domain owned by the group or any of its ancestors resolves, not only the group's own. | Human-only, read-only (no fresh-auth) |
| GET | `/api/edge/webapp/onboarding/options?group=<id>` (or `group_intent=new`) | Selectable buckets, environments, `github_connected`, this installation's `destination`/`destination_error`, and the group's `apps_domain`/`apps_domain_error` (below) for a group. | Human-only |
| POST | `/api/edge/webapp/onboarding/create` | Create the onboarding operation. | Human-only, fresh-auth |
| GET | `/api/edge/webapp/onboarding/detail?operation=<uuid>` | Poll the versioned, secret-free operation state. | Human-only |
| POST | `/api/edge/webapp/onboarding/choose` | Submit the choice for the current step (`address` / `github` / `verify`), matching the returned `revision`. `github` accepts `{"skip": true}` — GitHub deploys are optional and can be set up later from the app's own page — and `verify` accepts `{}`, the final "is it serving?" check needing no input. | Human-only, fresh-auth. **No transport retry** |
| POST | `/api/edge/webapp/onboarding/cancel` | Abandon the operation. | Human-only, fresh-auth |
| POST | `/api/edge/webapp/onboarding/workflow` | Return the generated GitHub workflow YAML for one WebApp; optionally mint or rotate `MOJO_DEPLOY_KEY` once (with `action` + a fresh `operation_id`). Works with **no repository configured** — the response's `repository` is then `null`, since the YAML never names one. | Human-only, fresh-auth |

**A `configuration_required` verdict, or a null `options.destination` with a
non-null `destination_error`, means this installation cannot serve app
addresses yet** — it is not a problem with the address typed. Route the
operator to System Setup rather than asking them to fix DNS. `create` also
refuses with a 400 in this state, before the WebApp exists and before a
purchase could move money; check for it on the connected-domain and purchase
paths, which never see a precheck verdict. A resolved `destination` (from
either precheck's `ready` or `options`) is `{type: "CNAME", value, provenance}`,
`provenance` one of `override` or `platform_base_url`. Only an **external**
(`mojo`-provider) domain's `records_needed`/`ready` verdict carries `records`
to publish yourself — a managed domain never does.

**`options.apps_domain`** is `{id, name, provider}` for the writable domain
(the group's own, or an ancestor's) new apps in this group go live under with
**zero DNS work**, or `null` with a plain-language `apps_domain_error` when
none qualifies or more than one candidate is ambiguous. A non-null
`apps_domain` is what unlocks name-only quick create in the Admin portal: pick
a name, and the address, HTTPS, and a starter page all come from onboarding
against that domain with no address step shown. A `null` `apps_domain` still
onboards through the full address-first flow below.

**Do not auto-retry `choose`.** It carries provider-affecting intent, and a
provider can accept a mutation while losing the response. Reload `detail` and
let the server reconcile durable intent against authoritative inventory rather
than replaying the mutation. A domain-purchase choice additionally carries a
one-use `confirm_token` consumed synchronously; it never appears in a later
response.

### Day-2 management

| Method | Endpoint | Purpose | Access |
|---|---|---|---|
| GET | `/api/edge/webapp/summary?webapp=<id>` | Frozen v1 read model: address + domain + `certificate` (`status`, `not_after`; null only without a vhost) + `address.aliases` (the app's extra addresses, `{hostname, certificate}` each — always a list, never null), `current_release`, `latest_deployment`, and deploy-key readiness. Secret-free — never a token, certificate key, or internal state. `schema_version` stays `1`: v1 grows additively only. | Human-only; `view_dns` / `manage_dns` / `security` + object access |
| GET | `/api/edge/webapp/summaries` | Bounded list projection for the merged Admin Deployments lane: one slim summary-v1-subset item per visible app (`webapp` identity, `address` + `certificate`, `current_release`, `latest_deployment`), ordered by slug, `{schema_version: 1, items, count, limit: 50, truncated}`. Scope is exactly the REST list's — global-or-group `VIEW_PERMS`, always intersected with a caller-supplied `?group=`. | Human-only (key-backed sessions refused); `view_dns` / `manage_dns` / `security` globally or in at least one group |
| GET | `/api/edge/webapp/deployment?webapp=<id>` (and `/deployment/<pk>`) | Deployment (fleet-convergence) history, group-scoped. Read-only; a cross-tenant id is not readable. | `view_dns` / `manage_dns` / `security` |
| POST | `/api/edge/webapp/rollback` | Repoint the app at an already-verified earlier release. Body `{webapp, release}`. A foreign release id 404s; a `pending` (unverified) release is refused. | Human-only (CI keys denied), fresh-auth; `manage_webapp` + explicit object check |
| POST | `/api/edge/webapp/detach_address` | Take the app offline: unlink and delete its serving vhost, keep the app and its release history. Every extra ("alias") address is removed with it. | Human-only, fresh-auth; `manage_webapp` |
| POST | `/api/edge/webapp/attach_domain` | Point one more address you own at this app. Body `{webapp, hostname, retry_certificate?}`. Returns `{webapp, status, hostname, reason, dns, ...}` — see [the status table](#attaching-your-own-domain-to-an-app) below. Safe to call again — that *is* the "check again" action — and it makes at most one provider write per call. An address that can never work here (a wildcard, the bare domain, a deeper name, one already serving something else) is an error, not a status. | Human-only (CI keys denied), fresh-auth; `manage_webapp` + explicit object check |
| GET | `/api/edge/webapp/attach_preview?webapp=<id>&hostname=<name>` | What `attach_domain` **would** do with that hostname, without doing any of it. Returns `{webapp, status, hostname, ...}`: `ready` (plus `dns` and `domain: {id, name}`), `needs_domain` (plus the same `reason` the write returns), or `unusable` (plus the sentence the write would have refused with). Free to call on every keystroke — no DNS lookup, no certificate work, no write — and it deliberately does **not** say whether the address is already taken; the write answers that at submit. **No step-up — it is a read**, but it carries the *write's* permission because it is a question about a write. | Human-only (CI keys denied); `manage_webapp` + explicit object check |
| POST | `/api/edge/webapp/detach_domain` | Remove one extra address. Body `{webapp, vhost}` — the `vhost` id from the address list. Returns `{webapp, status: "detached", hostname}`. The app's own address and another app's address both 404 (the same non-disclosing answer either way). The app, its certificate and its releases are untouched — take the app's *own* address down with `detach_address` instead. | Human-only, fresh-auth; `manage_webapp` + explicit object check |
| GET | `/api/edge/webapp/aliases?webapp=<id>` | Every address this app answers on: `{webapp, addresses: [{role, vhost, hostname, domain: {id, name, provider}, dns, enabled, certificate}]}`, the app's own address first (`role: "primary"`, then `"alias"`). `certificate` is `{status, not_after}` or null. No step-up — it is a read — and no live DNS lookup, so `dns` is the domain's mode, not a per-name probe. | Human-only; `view_dns` / `manage_dns` / `security` + object access |
| GET | `/api/edge/webapp/health?webapp=<id>` | On-demand public HTTPS reachability of the live address: `healthy` / `unhealthy` / `not_configured`. Never echoes a raw probe error. | `view_dns` / `manage_dns` / `security` |

Deleting a WebApp (`DELETE /api/edge/webapp/<pk>`) tears down its serving vhost
and deactivates/unlinks its `MOJO_DEPLOY_KEY` in the **same** transaction as the
row delete; release bytes in S3 are intentionally left. See the
[backend day-2 reference](../../django_developer/edge/webapps.md#day-2-management).

### Attaching your own domain to an app

An app always has exactly **one** address of its own — the one onboarding gave
it. `attach_domain` adds **extra** addresses ("aliases") that serve the
identical release. The app's own address never moves; changing it is still the
change-address flow.

`attach_domain` is a **status machine you re-enter**, not a one-shot. Call it,
render what comes back, and call it again with the same `hostname` when the
user presses Check. Never poll it on a timer — a call can do provider work.

| `status` | What it means | What to render |
|---|---|---|
| `needs_domain` | No domain connected here covers that hostname. | The `reason`, and a link to the Domains page. There are **no** `records` — nothing can be published until the domain itself is connected, and the API will not guess-and-delegate a parent zone on your behalf. |
| `records_needed` | Your DNS is elsewhere and the record isn't published yet. | The `records` array — `{type, name, value, ttl}`, copy-paste verbatim — plus a Check button that re-calls with the same hostname. Behind a proxy (Cloudflare) the record must be **DNS only** / grey cloud or the check never passes. |
| `certificate_pending` | HTTPS issuance is in flight. | The `reason` and a Check button. Minutes, not seconds. |
| `certificate_failed` | Issuance failed. | The `reason`, the repair `records`, **and a separate explicit "Try again"** that re-calls with `retry_certificate: true`. |
| `attached` | Live and serving. `created` is `false` when it already was. | Success. Re-calling is a no-op, which is why Check is free to press. |

Every response also carries `hostname`, a plain-language `reason` (show it as
written — it is the server's sentence, and a refusal comes back the same way),
and `dns`: **`managed`** (the platform writes this domain's DNS records for
you) or **`external`** (you publish them). Key your copy on `dns`, not on the
shape of the hostname.

**`retry_certificate` is the only thing that re-requests a failed
certificate**, and only your explicit repair button should set it — a plain
Check must never mint a new certificate order. It is parsed strictly: a real
boolean `true`, or the strings `"1"` / `"true"` / `"yes"` / `"on"`. Anything
else — including the string `"false"` — is False.

What is refused outright (an error response, not a status), because retrying
cannot help: a wildcard (`*.example.com`), the bare domain (use
`www.example.com`), a deeper name (`a.b.example.com` — one label only, so every
extra address stays inside the domain's existing certificate), a name already
serving anything else here, and — on a managed domain — a name that already
carries other DNS records or points somewhere else.

If the domain belongs to a **parent** workspace rather than this app's own, you
need manage authority in the workspace that owns the domain; reading it is not
enough to write a record or request a certificate against it.

**Tell the user which case they are in before they submit.** `attach_preview`
is the same decision without the write, so the built-in Admin's add-an-address
dialog calls it as the address is typed and says whether the platform will
publish the record (`dns: "managed"`), whether the user will (`"external"`),
whether the domain needs connecting first (`needs_domain`), or whether the
address can never work (`unusable`) — all before Add is pressed. It never
claims the address is free; only the write knows that. If your own client does
the same, key the copy on `status` and `dns`, exactly as you would for the
write's response.

### Deploy key

| Method | Endpoint | Purpose | Access |
|---|---|---|---|
| POST | `/api/edge/webapp/link_key` | Mint or rotate the site's CI credential (explicit `action` `mint`/`rotate` + client `operation_id` UUID). The token is returned **once**; a replay returns the receipt with `token: null`. Rotation is a hard cutover — the old key dies immediately. | Human-only, fresh-auth (600s); `manage_webapp` + object access |
| GET | `/api/edge/webapp/key_status?webapp=<id>` | Safe linked/active, timestamp, and last-use metadata. Never returns a token. | `view_dns` / `manage_dns` / `security` + object access |
| POST | `/api/edge/webapp/revoke_key` | Deactivate and unlink the key. Stops future releases; changes nothing currently served. | Human-only, fresh-auth (300s); `manage_webapp` + object access |

## Built-in Admin workflow

The packaged Admin exposes permanent Upstreams, Vhosts, and Routes pages over
these APIs. Its Vhost wizard offers only `api`, `site`, `site_api`, and
`redirect`, derives the fields for the selected shape, and never accepts nginx
configuration. Platform administrators can declare or retire Upstreams; an
existing destination is never repointed.

Routes requested with a new `site_api` Vhost are created one at a time. If one
fails, successful rows remain visible and the Routes page repairs only the
missing desired rows. The UI reloads authoritative Vhost/Route state and the
System Setup hosting proof rather than treating a successful POST or publish
receipt as fleet convergence.
