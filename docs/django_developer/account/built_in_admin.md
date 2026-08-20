# Built-in Admin Portal

django-mojo ships a small, dependency-free control-plane UI. It covers System
Setup/readiness, the Dashboard, User and Group CRUD, Domains, provider
credentials, live DNS record sets, Certificates, Upstreams, Vhosts, Routes,
and WebApp GitHub deployment-key management. Fleet, operations, security,
configuration, and metrics extend the same shell without changing its delivery
boundary.

## Enable and route it

The portal is loaded with the account app and defaults to `/admin/`. The path
and source-session lifetime are static deployment settings:

```python
MOJO_ADMIN_PATH = "admin"          # one URL-safe path segment
MOJO_ADMIN_SESSION_TTL = 900       # seconds
MOJO_ADMIN_COOKIE_NAME = "mojo_admin"
MOJO_ADMIN_COOKIE_SECURE = True    # defaults to not DEBUG
```

An interactive JWT user needs a global `view_admin`, `manage_users`,
`manage_settings`, or
`admin` permission (superusers pass automatically). API keys and group-scoped
tokens cannot create an Admin source session.

## Private source delivery

An anonymous `GET /admin/` receives only a small Bouncer handoff page. It does
not receive the shell, page declarations, forms, or private CSS. Anonymous
requests for `/admin/assets/*` return `404`.

After Bouncer login, the gate exchanges the already-validated interactive JWT
at `POST /api/account/admin/session`. The server writes a short-lived Redis
record and a path-scoped `HttpOnly; SameSite=Strict` cookie. Every shell and
asset request revalidates the Redis session, the active User, the recorded JWT
expiry, and the fingerprint of `User.auth_key`. Changing the auth key therefore
revokes both JWTs and Admin source access.

Private responses use `Cache-Control: no-store`, a restrictive CSP, frame
denial, and MIME-sniffing denial. Production should terminate TLS and keep
`MOJO_ADMIN_COOKIE_SECURE=True`.

This is a delivery boundary, not DRM: an authorized operator or anyone with a
build artifact can inspect frontend source. REST permissions remain the
authority for every data read and mutation.

## Browser architecture

The portal is native HTML, CSS, and ES modules packaged inside `mojo`. The
shell owns authentication, theme, navigation, routing, stale-render
cancellation, overlay cleanup, and heading focus. A fixed registry imports the
seven feature namespaces `dashboard`, `people`, `webapps`, `activity`,
`platform`, `advanced`, and `settings`; there is no runtime plugin discovery or
settings-based module import. Each descriptor declares its routes, navigation,
stylesheet, title, capability check, and one
`render({ctx, route, navigate, signal})` function. Render must resolve to
exactly one DOM `Node`; an optional `node.dispose()` releases feature-local
listeners or work.

Primary navigation is Dashboard, Deployments, Domains & DNS, Serving, People,
Activity, Metrics, Maintenance, and Settings, then System Setup in its own
**System** group at the bottom. A feature contributes as many sidebar entries
as its own capabilities allow, so the entry count is not the feature count:
Domains & DNS and Serving are two permission-gated Advanced entries and stay
beside Deployments because domains, public records and serving shapes are
ongoing application controls, while Metrics, Maintenance and System Setup are
three separate Platform entries on three different grants. **Serving** is gated
on `manage_network` alone — the pages behind it create, retire and repoint
serving rows across every tenant, which a read grant must not see — and it
covers the `vhosts`, `routes` and `upstreams` routes. Deployments (the `webapps` feature — the id is unchanged,
only its routes and label) is the one place for everything running on the
fleet: the API service and django-mojo framework rows on top, one row per web
app below. It owns the `deployments` and legacy `webapps` routes; `#/webapps`
canonicalizes to `#/deployments`.

**There is no Platform page.** Its health grid and the Advanced diagnostics
page were dissolved into the Dashboard rows, each row carrying a Details
drill-in over the same evidence; deployment history and its same-SHA recovery
live in the Deployments lane. See
[Dashboard integration](admin_portal/dashboard.md) for the permissioned source
matrix, the drill-in layer, and canonical cross-feature route state.

A navigation entry may declare a numeric `order`; `navigationFor` sorts the
flattened entry list by it, and the sort is stable, so an entry without one
keeps its descriptor position. An entry may also declare `badge: true`, which
renders a small amber `.nav-badge` dot inside the sidebar link (labelled
"Needs attention" for assistive technology). System Setup is the only entry
using either today: `order: 100` puts installation work below daily work, and
its badge follows `features.platform.capabilities.setup_attention`.

Python publishes the same fixed namespaces at `bootstrap.features`, alongside
the stable flat `bootstrap.capabilities` compatibility object. Provider output
is validated as named boolean capabilities. An exception or malformed provider
disables only that feature and logs its exception class, never request data or
secrets.

Deployments uses two additive bootstrap fields instead of the shared membership
list: `webapp_groups` is the case-insensitively sorted set of effectively active
groups the caller may manage, each row includes `can_manage_dns`, and
`can_create_webapp_group` says whether the wizard may offer **Create New
Group**. The address step uses that selected-group flag for managed-domain and
purchase controls; it does not substitute the flat global network capability.
Global WebApp authority means
`security`, or both `manage_webapp` and `manage_dns`; the same two-part check
may instead be supplied by an exact or inherited GroupMember grant. A
non-superuser creating a group additionally needs global `manage_groups` or
`groups`. Superusers pass all of these checks. Admin admission, emitted UI
capabilities, and literal `permissions.admin` are not backend authority.

The merged Deployments list reads `GET /api/edge/webapp/summaries` — a bounded
(50-row) slim projection of summary v1 (webapp identity, address +
certificate, current release, latest deployment) at flat query cost, scoped to
exactly the rows the caller's `GET /api/edge/webapp` list would return
(including the unconditional `request.group` intersection). The full
`webapp/summary` stays the per-app drill-in contract, and its `address` block
now additively carries `certificate` (`status`, `not_after`). The lane's API
section reads `GET /api/account/admin/platform?sections=deployments,api` and
`GET /api/account/admin/platform/framework`; its update and hold controls call
the existing framework-update and advanced-settings `framework_pin` writers —
one mechanism, surfaced where the operator looks.

Three things the merged list says that it used to leave the operator to work
out. **The headline names the failing thing**, in priority order: the API
service's own failed deploy (with its node counts, what is still converged, and
a **Retry same SHA** that is the existing platform `deploy/retry` verb behind
its own manage gate), then a web app whose latest deployment is `failed` or
`rolled_back`, then any other red or amber row — and those last two quote the
row's own `.row-name`/`.row-value`, so the banner can never contradict the row
it points at. A web-app failure gets **no node counts**: a `WebAppDeployment`
records `{runner, job}` targets, not a fleet roster, and it gets no Retry
either, because the only path forward is the fresh-auth, reason-required
`webapp/rollback` that already lives on the app's Deploys tab.

**Each row says how its live build arrived** — "via GitHub push", "via CLI or
API", "via upload", or "source not recorded" — from `current_release.source`.
And the **Web apps section carries a subhead** built from the response's
`fleet` block: how many are live, the domain they answer on, and the one
certificate behind them. A truncated list drops the domain and certificate
clauses and carries the "showing the first N" sentence instead, which used to
sit on the headline and read as if it were about the failure.

Both slots are additive on the shared row components: `rowSection(label, rows,
{sub})` and `statusHeadline({..., actions})`. Nothing inside `statusHeadline`
may carry `role="status"`, `aria-live`, or a loading/error state — `onRefresh`
replaces that whole block, so an announcement made there is destroyed before it
speaks; `runAction`'s document-level live region is what survives.

The wizard defaults to the nonnumeric Create New Group sentinel only when that
flag is true; otherwise it selects the first eligible existing group and never
coerces an empty value to id `0`. A draft stores one client UUID in
origin-scoped `sessionStorage`. At first submission it also freezes the exact
nonsecret request payload. On modal reopen or reload, a submitted draft first
queries operation detail by UUID: an existing receipt clears the draft and
mounts authoritative state. If detail is absent or temporarily unavailable,
only that exact frozen payload can be replayed; all identity controls remain
disabled. Editing requires explicit **Start over**, which abandons the pending
UUID and creates a fresh draft.

Shared `TableView`, `FormView`, overlay, model, relationship, icon, API, and
view-state primitives live under `assets/components/` and `assets/core.js`.
The full-envelope API helper preserves `data`, `results`, `items`, `count`,
`start`, and `size`; use it for paged relationships instead of discarding REST
metadata. The relationship control URL-encodes searches, filters, graph names,
and detail identifiers; debounces search, cancels stale requests, supports
paging and keyboard listbox behavior, and posts only its selected id through a
hidden input. The ordinary API wrapper still refreshes an expired JWT once and
renews the source session. HTTP `440` now opens an in-portal recent-auth modal
for the signed-in username. A successful password or passkey check replaces
the stored JWT and retries the blocked request exactly once; concurrent 440s
share the same prompt. Cancel preserves the still-valid session and page. An
incomplete password login (for example, one that still needs MFA or a forced
password change) is not accepted as recent authentication and does not retry
the request. Route and Setup loads use reduced-motion-aware skeletons;
stateful Setup calls hold a tokenized, non-dismissible busy layer and release
it on completion, rejection, abort, or the 440 prompt.

The Hybrid visual density is intentional: 13px base text, 11–12px tables and
forms, 21px page titles, and 20px KPI values. Light, dark, and system themes are
built in and stored only in browser local storage. The responsive shell keeps
keyboard-visible controls, traps focus in modals, restores focus on close, and
uses text nodes for API data; only the fixed local SVG icon catalog uses HTML.

The Platform feature owns exactly three routes — `setup`, `metrics`, and
`maintenance` — work an operator starts on purpose. The evidence it used to
render (API/service health, fleet, certificate and security posture) is
summarized on the Dashboard rows and reachable in full through their Details
drill-ins; UUID-addressed deployment history and its recovery controls render
in the Deployments lane. Its private `assets/features/platform/page.js` module
is now the System Setup journey alone: the normalized readiness report, durable
step progress, typed late choices, cancellation, and bounded live log. See
[System Setup and Readiness](system_setup.md) for the service and security
contracts.

Platform also owns the `metrics` route — CloudWatch line charts for EC2, RDS,
and ElastiCache, gated on `manage_aws` and rendered by
`assets/features/platform/metrics.js` and `chart.js`. See
[Admin Metrics](admin_portal/metrics.md).

See [Platform and Advanced Admin controls](admin_portal/platform.md) for the
dedicated permissions and bounded evidence contract.

The Advanced feature owns two first-class destinations — Domains & DNS, and
**Serving** (`#/vhosts`, labelled "Serving" and gated on `manage_network`
alone) — plus the raw Credentials, Certificates, Upstreams and Routes pages.
Serving and Routes had no navigation entry at all until the app-scoped Serving
tab shipped; they were reachable only by typing the URL, and the app page's
Serving tab now links into them for operators. These are operator surfaces:
their inner copy uses the platform's own vocabulary, which the customer-facing
WebApps vocabulary gate deliberately does not police. Its
raw-evidence `advanced` route is gone with the Platform page that linked to it;
`advanced_overview()` and `GET /api/account/admin/advanced` are unchanged and
still served, but no portal surface reads them, so `view_advanced`,
`view_advanced_inventory`, and `view_advanced_security` are API-only grants
today. Its `assets/features/advanced/page.js` module is
the permanent hosting UI. It does not
create portal-only mutation endpoints. Provider DNS writes are keyed
single-flight and receive no automatic transport retry. Every write is
followed by a fresh authoritative
record read; an unconfirmed outcome latches that record set as
refresh-required until the operator explicitly refreshes it. Vhost creation is
a four-shape wizard (`api`, `site`, `site_api`, `redirect`); `site_api` routes
are created sequentially so a partial result can be repaired without replaying
successful rows.

The domain detail page adds two read-only surfaces and **no portal-only
endpoints**. A capability-gated **role line** reads
`GET /api/edge/webapp/onboarding/options?group=<id>` — only for a
group-scoped domain, and only with `manage_webapps` — and badges the domain
**Apps domain** when that workspace's `apps_domain` is this one; a DNS-only
operator never makes the call and simply sees no badge. A **What's on this
domain** overview panel joins the existing `GET /api/dnsman/dns`,
`GET /api/edge/vhost` and `GET /api/edge/webapp` reads into the wildcard
record, the addresses on the domain (matched by exact `domain.id`, never a name
suffix) and the MX/TXT rows an app going live never touches. The zone read is
skipped outright for a `mojo`-provider or non-`active` domain — there is no
zone here to read — and each read is caught independently so one failure leaves
the other blocks standing. Its loading state renders into a body node, not over
the panel heading, like every other panel-scoped load in the lane.

The Settings feature owns ongoing framework configuration and the typed
AUTH_CONFIG/expected-fleet browser controls. See
[Admin Settings catalog](admin_portal/settings.md). Advanced contributes only
the Domains & DNS sidebar entry and keeps no duplicate settings form.

The People feature owns the complete User and Group operator journey. Its
capabilities are issued by the backend per operation (view/manage Users,
view/manage Groups, API-key custody, sign-in evidence, and each Activity lane);
rendered controls are never authorization. See [People](admin_portal/people.md)
for the custom fresh-auth actions, versioned permission map, and secret rules.

## Packaging and testing

Portal assets live under `mojo/apps/account/admin_portal/` rather than Django's
public static pipeline. Delivery is generated from the exact base
`manifest.json` and the six owner-local `assets/features/*/manifest.json`
files. A feature manifest may name only files in its own directory; duplicate,
missing, unknown, absolute, backslash, dot, and traversal paths fail closed.
Add a shared asset to the foundation manifest or a feature asset to that
feature's manifest. Do not add a second Python allowlist.

Shared browser behaviour is packaged the same way. `assets/components/actions.js`
carries the portal-wide responsiveness affordances — the pending state, the
clipboard control, and the loading/error wrapper every panel paints through —
and is declared in the foundation `manifest.json`. An undeclared asset fails
silently: `load_manifest` raises only for a *declared but absent* file, so a
module on disk that nobody declared is simply never served, and the browser
404s it. See
[admin_portal/responsiveness.md](admin_portal/responsiveness.md) for the policy,
the rule about where a pending state may be attached, and the banned-pattern
test that enforces it.

Use `bin/create_testproject` and test through the real HTTP endpoints. The
split tests in `tests/test_account/test_admin_portal_*.py` prove anonymous
gate-only delivery, exact asset denial, cookie attributes, authorized delivery,
feature-provider isolation, shell/component contracts, auth-key revocation,
forced-reauth behavior, and WebApp key lifecycle.

For pixel review without weakening Bouncer, run `bin/admin_preview` and open
`http://127.0.0.1:5608/`. It serves the exact packaged assets with deterministic
loopback-only API fixtures; it does not add a Django route or production bypass.
The launcher is intentionally thin; `bin/admin_preview_support/` owns the
support server, foundation gallery, and resettable feature providers. Use
`--key-state missing|active|rotated|revoked` to exercise the four WebApp
deployment-key presentations, `--onboarding-state
idle|address|github|verify|complete|lost_key|new_group` for WebApp onboarding
(the last state has zero memberships and loses the first committed response),
`--setup-state idle|choice|delay|error|fresh|ambiguous` for resumable Setup and
its busy/error/440/lost-response states,
`--dashboard-state healthy|degraded|down|denied|unknown` for Dashboard source
states (`down` proves a failure and reddens the headline; `degraded` is amber
evidence with availability still green),
`--metrics-state live|empty|unconfigured|denied|partial` for the CloudWatch
Metrics page (`partial` keeps EC2 and ElastiCache live while RDS degrades),
`--deployments-state mixed|converged|failed|empty` for the merged Deployments
lane (`mixed` pairs an in-flight API attempt with one green app and one
no-address app; `failed` pairs a failed deploy with an expired certificate;
framework behind/pinned states come from `--maintenance-state`),
and `--port` when parallel work needs isolation. Every launch resets mutable
provider state before serving.

To QA local Admin source against a real installation without deploying it:

```bash
bin/admin_preview --port 8766 --upstream https://api.example.com
```

Open `http://localhost:8766/admin/` and use the installation's normal password
flow. The bridge accepts only one public HTTPS hostname origin, checks every DNS
answer on every request, pins the TLS peer while retaining hostname
verification, bounds traffic, and never follows redirects. Each preview browser
receives an unguessable HttpOnly token; Host/Origin/fetch-metadata checks gate
every proxied request. That token owns a separate, path/domain/expiry-aware
upstream cookie jar inside the preview process, and upstream cookies are never
set on localhost. No credential is accepted on the command line and
headers/bodies are not logged. External OAuth callbacks are intentionally not
bridged.
