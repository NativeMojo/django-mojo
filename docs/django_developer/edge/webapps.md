# Web apps and releases

The companion to [vhosts](README.md): that decides *how* a domain is served,
this decides *what* is being served.

Two problems it exists to solve.

**Upload and desired state used to be two unrelated mechanisms.** With CI
writing a `current` pointer in S3 there was no fleet proof or safe rollback.
Here the linked key is scoped to one WebApp, S3 verifies immutable bytes, and
django-mojo owns deployment, convergence status, and rollback.

**There was no answer to "what is deployed right now?"** It was a symlink on N
nodes; you would SSH to each to be certain, and rollback meant rewriting an S3
object, which needs AWS access — exactly the access this removes.

## The rule: the database is authoritative, S3 is just bytes

There is deliberately **no `current` pointer object in S3**. Two sources of
truth disagree eventually and nobody can tell which won. `WebApp.current_release`
is the answer, and nodes converge on it through the same generation lifecycle
that installs vhosts and certificates — not a second mechanism.

## Models

```
WebApp
  group            owns it
  slug             a LABEL, not a path
  vhost            FK, nullable — the PRIMARY address
  alias_vhosts     reverse of Vhost.alias_of — extra addresses
  bucket           from EDGE_RELEASE_BUCKETS
  prefix           DERIVED: webapps/<group>/<id>
  api_key          the CI credential, one per site (OneToOne)
  current_release  what nodes should serve

WebAppRelease
  webapp, version (unique together), manifest, status, source, created_by
  status: pending -> uploaded -> live -> superseded
  source: github | api | upload | unknown  (how it ARRIVED)

WebAppDeployment
  release, previous_release, status, targets, rollback_targets, detail
  status: queued -> deploying -> live
                       \-> rolling_back -> rolled_back / failed
```

**`WebAppRelease.source` is derived at the boundary, never claimed.** Once the
row exists, a GitHub push, a CLI call and a browser upload look identical, so
`POST /api/edge/release` decides the class while it can still see the caller:

| Caller | Body | Stored |
|---|---|---|
| interactive session (the portal's Upload-a-build tab) | anything | `upload` |
| ApiKey, site has `github_repository`, body `source: "github"` | the marker | `github` |
| ApiKey, any other case | anything | `api` |
| registered before this field existed | — | `unknown` |

The client may only refine **within** the class its credential already proved:
no session can claim `github`, no key can claim `upload`, and the marker on a
site with no GitHub repository stays `api`. Any other hint value is ignored
rather than refused — the marker is additive, so a stale client must still be
able to deploy. `source` is in `NO_SAVE_FIELDS` beside `status`, and
`releases.register()` stamps it only on the creating call: the reuse branch
returns the stored row untouched, because a re-registration that could restamp
would rewrite how an already deployed build got here. "upload" is the design's
word for the whole interactive class — a hand `curl` on a logged-in session is
`upload` too, since what it names is the credential, not the tool.

**`slug` is a label.** Nothing on disk is named after it — a vhost's web root
comes from `Vhost.pk`. That is what stops one tenant pointing a vhost at
another tenant's installed build output, which a shared string namespace would
have allowed.

**`slug` is unique per group, not globally.** A global constraint lets one
tenant squat another's intended slug, and the duplicate error leaks that it
exists.

**A web app's vhost must sit on a domain owned by its group, or by a group
above it.** The ancestor half is what lets one domain carry several teams:

```
  MojoVerify          owns mojoverify.com  +  its wildcard certificate
   ├── api team       owns the api web app
   └── portal team    owns the portal web app
```

Both teams publish under the one domain, with one certificate to renew and the
private key in one place. **Siblings stay isolated** — neither is above the
other, so the portal team cannot attach a web app to the api team's vhost, and
`get_member_for_user` does not grant a child's members anything in a sibling.
Only ancestors, never descendants and never siblings.

Two refusals this deliberately keeps. An **unrelated** group still cannot
attach a web app to another group's vhost. And a **house** domain (`group` is
null) is nobody's ancestor, so it is still refused outright — that is the
finding this check was written for: a global `manage_dns` holder could
otherwise attach the platform's own vhost to a web app in a group they control
and serve their content on the platform's hostname.

> Consequence worth planning around: a domain has exactly **one** owner, so
> per-team groups only isolate teams that publish under *different* domains, or
> that sit under a shared parent as above. Creating a site's vhost the first
> time needs access to the owning domain, so an admin does that once per site;
> day-to-day publishing then runs on the web app's own API key.

**`bucket` and `prefix` are not caller-controllable, and this is load-bearing.**
The API mints presigned uploads signed with the platform's *static* AWS
credentials (`mojo/helpers/aws/s3.py` holds one global `S3Config`, shared with
KMS). A writable `bucket` would let a tenant holding `manage_dns` name any
bucket those credentials can reach — fileman, terraform state, logs — and have
us sign writes into it. So `bucket` comes from an allowlist, `prefix` is
derived, and both are in `NO_SAVE_FIELDS`.

**`pending` is a real state.** A release that is registered but unverified must
never be promotable; an abandoned CI run would otherwise leave a row that looks
shippable and 404s in production.

**Releases are immutable.** Rollback depends on it: a version that could be
re-registered would silently change what an older, still-referenced row means.
Rows are neither editable nor deletable.

## The upload flow

```
1. CI:  POST /api/edge/release          {webapp, version, manifest}
        -> WebAppRelease(status=pending)
        -> one presigned PUT per manifest entry
2. CI:  PUT <url>                        straight to S3
3. CI:  POST /api/edge/release/complete  {release}
        -> HeadObject per entry; compare checksum and size
        -> status=uploaded
4. django-mojo creates WebAppDeployment and targets every active edge runner
5. CI:  GET /api/edge/release/deployment/<id> until live or terminal failure
```

The canonical GitHub action retries the current presigned PUT when S3 returns
a transient HTTP status or the runner encounters a timeout, connection reset,
or remote disconnect. Backoff is bounded; permanent HTTP failures still fail
immediately. The generated workflow versions releases with the commit SHA,
GitHub run id, and run-attempt number. Retries inside one attempt reuse its
immutable release, while a workflow rerun creates a distinct release even for
the same commit.

Manifest paths are relative and slash-delimited. Each segment accepts letters,
digits, `.`, `_`, `-`, `!`, and `~`; the last two are required by Next.js
static exports. Empty, absolute, `.`/`..`, control-character, space, and other
shell/metacharacter paths are refused before an upload URL is minted.

### Why presigned PUTs rather than STS session credentials

The original design called for minting short-lived STS credentials scoped to
the release prefix. Two reasons this does not:

- **There is no STS support in `mojo/helpers/aws/`.** That flow needs a new
  helper, a new IAM role and a terraform change before a line of it is testable.
- **A session policy over `releases/<id>/*` still permits arbitrary keys under
  that prefix.** A presigned PUT permits exactly one key — and CI must declare
  the manifest before uploading anyway, so every key is already known.

The honest trade: a presigned URL is tighter **per-object** and wider
**per-signer**, since it carries the authority of the platform's static
credentials rather than a scoped role. That is precisely why `bucket` is
allowlisted and `prefix` derived.

### Integrity is S3's, not ours

Each presigned PUT binds `x-amz-checksum-sha256` into the signature, so **S3
itself rejects a body that does not hash to the declared value**. `complete`
reads the stored checksum back with `HeadObject` — it never pulls the bytes
through this process, and it never trusts the client's word.

Three things that will bite an implementer:

- **`ChecksumSHA256` is base64 of the raw digest; a manifest carries hex.**
  `releases.hex_to_b64` does the conversion. Passing hex through produces a
  signature no upload can satisfy.
- **An object with NO stored checksum is a failure, not a pass.** It means the
  upload bypassed the bound URL, and treating absent as "fine" removes the
  verification entirely.
- **HeadObject returns `ChecksumSHA256` only when asked.** The request must
  pass `ChecksumMode="ENABLED"` (`s3.head_object` does); without it the field
  is absent no matter what is stored, and the previous bullet turns every
  verification into an unconditional failure.

## GitHub completion is the deployment control plane

Verified release completion sets desired state under a row lock, creates a
durable deployment, and then targets every runner that is alive on the `edge`
channel. A node proves the specific vhost was neither excluded nor left in
`www_pending`. Only after all targets complete does the deployment become
`live`.

Any target failure or timeout restores the prior release under the same row
lock and converges that rollback across the same runner snapshot. The rollback
checks that the failed release is still current, so an old deployment can
never overwrite a newer one. Periodic and startup convergence remain the
backstop for nodes that were offline during the snapshot.

There is no manual hold or promotion endpoint. The protected GitHub branch is
the human control plane. Intentional rollback is the human-only Admin action:
it repoints the site to an existing verified release and the same deployment
coordinator converges it. Rerunning a workflow for an older commit creates a
new immutable release and is a new deployment, not a rollback of release
state.

## Permissions

| Permission | Who | Reaches |
|---|---|---|
| `release_webapp` | the site's CI key | register, verify, automatic deploy, and that WebApp's deployment status |
| `manage_webapp` | an administrator | one-time key linkage and rotation |
| `manage_dns` | a site administrator | the `WebApp` row itself |

`POST /api/edge/webapp/link_key` requires explicit `action` (`mint` or
`rotate`) and a client-generated UUID `operation_id`. It returns the CI token
only for the first successful response. The raw token is removed from encrypted
storage before commit, so it cannot be recovered through the generic ApiKey
token graph. A retry with the same operation id returns its durable non-secret
receipt with `replayed: true` and `token: null`; if the first response was lost,
the operator must explicitly rotate.

Rotation is a **hard cutover** — the previous key is deactivated atomically,
with no grace window, because two live credentials for one site is exactly the
state that makes revocation unprovable. The endpoint requires an interactive
JWT authenticated within the last ten minutes and refuses API-key and
group-token sessions. Capture the returned token and immediately replace the
repository's `MOJO_DEPLOY_KEY` secret; a workflow caught between those
operations fails safely and can be rerun.

`GET /api/edge/webapp/key_status?webapp=<id>` returns safe linked/active,
timestamp, and last-use metadata; it never returns a token.
`POST /api/edge/webapp/revoke_key` accepts `webapp` and `operation_id`,
atomically deactivates and unlinks the key, and is replay-safe through the same
non-secret receipt model.

The cross-site check is an FK identity comparison (`request.api_key_id ==
webapp.api_key_id`), not a permissions lookup, and it **fails closed on null**:
a site with no linked key refuses every key rather than accepting any.

**Revoking a site's key stops future releases and changes nothing served.**
Desired state is driven by `current_release`, which has no dependency on the
key, so a compromised web-dev credential is contained by disabling one key with
no site going down and no emergency deploy. There is a test for it.

The standard GitHub secret name is **`MOJO_DEPLOY_KEY`**. The token belongs to
the repository's Actions secret store, not to developers' laptops. Merge/push
to the configured deployment branch is the authorization event.

### Durable onboarding

Admin → Deployments drives a resumable four-step operation: **WebApp → Domain &
DNS → GitHub → Go live**. The first screen collects only application identity;
repository and build settings wait until the GitHub step. At Domain & DNS the
operator selects a managed domain or buys a new one, and can open the permanent
Domains & DNS control to register an existing domain. The onboarding service
creates the exact CNAME itself. Certificate selection and renewal remain
automatic implementation details rather than a wizard choice.

`WebAppOnboardingOperation` stores versioned intent,
provider identity, bounded evidence, a revision, and a short lease. It never
stores a registrar confirmation token, GitHub installation token, deployment
key, certificate material, or raw provider error. `WebApp`, `Domain`,
`Certificate`, and `Vhost` remain authoritative for their resources.
The model and the WebApp profile fields (`display_name`, `environment`,
`github_repository`, `deployment_ref`, and `build_output`) land together in
`edge.0009_webapp_onboarding`, after `edge.0008_webappkeyoperation` and the DNS
reservation dependency.

The operation is actor-, group-, profile-, intent-, and origin-bound. Every
request, replay, and worker rechecks the active actor plus the full authority
contract: superuser, `security`, or both `manage_webapp` and `manage_dns`,
resolved globally or through the concrete group (including inherited member
grants). Literal `permissions.admin` is not a backend wildcard. API keys,
group tokens, and override-user key sessions are refused. Workers use atomic
leases and bounded backoff;
after an uncertain provider result they read authoritative inventory before
writing again. Cancellation stops recovery but preserves proved resources.

Options accepts exactly one of a positive integer `group` or
`group_intent=new`. New intent is state-free, reports no group-scoped GitHub
installation, and requires global `manage_groups`/`groups` as well as global
WebApp+DNS authority for a non-superuser. New create also requires a client
UUID and display name. One transaction creates the organization Group, UUID
receipt, and WebApp through `_advance_app()`, persists the derived
`webapps/<group>/<id>` prefix, and returns at `address`. A validation,
authorization, or storage failure rolls back all three. Concurrent UUID losers
leave their failed savepoint before reconciling the winner; concrete-group
callers separately retain the existing live `(group, replay_fingerprint)`
reconciliation and may still omit a UUID.

`options()` additionally carries `apps_domain` — `{id, name, provider}` for
the group's [apps domain](#the-apps-domain) (`webapp_apps_domain.resolve`), or
`None` — and `apps_domain_error`, a plain-language reason (`no_domain_reason`)
when it is `None`: no domain owned by the group or an ancestor, or more than
one candidate with no `BASE_URL`-suffix tiebreaker. This is what the quick-create
name field checks before offering "Create app" — a group with neither a
resolved apps domain nor an existing one still onboards through the
address-first path.

Serialized operations add `group: {id, name}`. Numeric floats are not accepted
as group identifiers. Replaying a UUID requires the same actor, origin,
normalized profile, and group intent; otherwise it fails. Admin persists the
exact first submitted payload. A reload queries detail before attempting an
exact replay, and a found receipt replaces the draft with authoritative state;
submitted identity fields stay immutable until explicit abandonment.
Cancellation or a later provider failure deliberately preserves a committed
Group+WebApp pair as recoverable application state—the atomic no-orphan
guarantee applies to the initial transaction, not later deletion.

Domain purchase still uses the registrar's live quote and typed domain/price
confirmation. The operation commits only a hash-bound purchase intent, then
the fresh-auth request consumes the raw one-use confirmation synchronously.
Workers never receive it. A lost response is recovered from `DomainPurchase`
and `Domain.metadata.purchase`, not by replaying money movement.

Guided DNS accepts exactly one concrete non-apex, non-wildcard label. It
inventories the complete record set, adopts an exact CNAME, and refuses mixed,
ambiguous, or foreign values.
The target is resolved by `webapp_destination.resolve()` — the explicit
`EDGE_WEBAPP_CNAME_TARGET` override, else a CNAME to the platform's own public
`BASE_URL` hostname (below) — never a blank value. A `*.{domain}` CNAME already
pointing at that destination covers every subdomain, so the address step writes
**nothing** in that case — a wildcard-covered domain onboards each app with
zero DNS writes. Certificate selection reuses an active exact/wildcard
certificate outside its renewal window; when none covers the hostname, every
provider issues the **apex-plus-wildcard** profile — one certificate per
domain, ever; the first app pays the issuance wait and every later app reuses
it. Private material never crosses the onboarding surface.

### URL-first entry and external domains

`GET /api/edge/webapp/onboarding/precheck?url=<address>` is a **stateless**
pre-flight: give it the address a user typed (`https://myapp.example.com`) and
it normalizes the URL, matches the hostname against the group's own domains,
derives the label, and returns a `verdict` before any operation is created.
Verdicts: `ready`, `records_needed`, `apex` (suggests `www.`), `deep_label`
(suggests one label), `path` (suggests a subdomain), `taken`, `conflict`,
`domain_unknown` (with an `options` block advertising whether external, purchase
and GoDaddy paths are available), `configuration_required` (the installation has
no serving destination yet — below), and `invalid`. Conflict detection is DB
checks plus **one** authoritative `probe.query_cname` — never a provider record
listing (which would enumerate a whole zone on shared credentials per
keystroke). It is group-scoped and non-disclosing: a domain in another group
returns `domain_unknown`, and an occupied address names the occupying app only
within the caller's own group. A probed CNAME answer that differs from the
resolved destination is confirmed against one random sibling label before it
counts as a `conflict`: an identical answer there means the response was
synthesized by a `*.{domain}` wildcard record, which a host-specific record
always overrides — so a wildcard pointing elsewhere never falsely blocks
onboarding, while a genuine host-specific foreign record still does.

**One resolver decides where every guided address points**
(`mojo.apps.edge.services.webapp_destination.resolve(hostname=None)`), used by
precheck, `options()`, `create`, and the address-advance step alike.
Precedence: the explicit `EDGE_WEBAPP_CNAME_TARGET` override, else a CNAME to
the platform's own public `BASE_URL` hostname — so an ordinary installation
needs zero destination configuration once `BASE_URL` is set. Set
`EDGE_WEBAPP_CNAME_TARGET` only for a split topology, where the tier serving
web apps is not the tier the platform's own hostname fronts; leave it unset
otherwise. Neither source resolving to a usable hostname raises
`DestinationUnavailable`, which precheck reports as `configuration_required`
and `create` refuses as a plain 400 **before** any WebApp — or a purchase that
would move money — is created. A hostname that resolves to the platform's own
address is a plain `invalid` instead: that is a bad request, not an unserveable
installation. See [Serving-destination readiness](#serving-destination-readiness)
below for how System Setup surfaces this ahead of onboarding.

A managed domain's `ready` verdict carries the resolved `destination` —
`{type: "CNAME", value, provenance}`, `provenance` one of `override` or
`platform_base_url` — and **no `records` key**: the platform writes that
record itself, so there is nothing to copy. External (`mojo`-provider)
`ready`/`records_needed` verdicts are unchanged and still carry `records`,
since that domain's DNS lives outside the platform.

**A domain whose DNS lives at an outside host works end to end**, with no
provider credential handed over and nothing to buy. Such a domain arrives
through the existing delegated-ACME flow (`POST /api/dnsman/delegation/initiate`
→ the user publishes one `_acme-challenge` CNAME → `POST
/api/dnsman/delegation/verify`) and becomes a `Domain(provider="mojo")`. The
address step recognizes `provider="mojo"`: it cannot write records (the `mojo`
provider has no DNS CRUD), so instead it shows the exact records to publish
(the app CNAME to the resolved destination plus the ACME-delegation CNAME) and
verifies the app CNAME authoritatively with `probe.query_cname`. The certificate
is the delegated **apex-plus-wildcard** profile (`certs.request_certificate`
with `names=None`), so one delegation and one certificate cover every app on
that domain; the wildcard is what the vhost's covering-certificate scan reuses.

**Waiting on the user is not an error budget.** When the address step is waiting
on a user-published record — the app CNAME is absent/mismatched, or a delegated
certificate failed because the `_acme-challenge` record is missing — the step
returns the internal `WAIT_FOR_USER` sentinel: the operation parks as `waiting`
with no scheduled retry and its attempt budget reset. The user re-checks by
re-submitting the address choice (which resets `attempts`), and a fresh
certificate is requested at most once per re-check, never in the retry loop. A
long-but-legitimate provider wait that would once have exhausted the retry
budget now parks the same way instead of failing an onboarding that just took a
while. Changing the address later is an **in-step vhost swap**: the old address
keeps serving until the new vhost and certificate are ready, then the old site
vhost is retired (its delete publishes convergence).

GitHub evidence uses only a `GitHubInstall` whose `group` exactly matches the
operation. Repository, ref, and build-output values are whitelist validated.
Evidence is honestly `verified`, explicit `attested`, or `unavailable`. The
generated workflow is a single file that references django-mojo's **public**
composite action at
`NativeMojo/django-mojo/examples/github/actions/deploy-webapp@main` and passes
`MOJO_DEPLOY_KEY` only through `${{ secrets.MOJO_DEPLOY_KEY }}` — no secret is
embedded, and the deploy logic runs on the GitHub runner, not the user's
machine. `workflow(web_app, api_origin)` takes the platform origin from the
same-origin request so `api-url` is concrete. (The earlier generator emitted
`python -m mojo_webapp deploy`, a module that exists nowhere; that is fixed.)

**GitHub is optional at both ends of that contract.** `_advance_github`
accepts choice `{"skip": true}`, or simply no repository anywhere (choice nor
profile) — either moves the cursor straight to `verify` with
`evidence.github = {"status": "skipped"}` and leaves the WebApp's `github_*`
fields untouched, so connecting a repository later starts clean.
`workflow(web_app, api_origin)` mirrors this: `web_app.github_repository`
being unset is not an error — `repository` is validated only when present, and
the serialized response carries `repository: null`. The generated YAML itself
is unaffected either way; it names no repository, only `ref`/`output`/
`api-url`/`webapp-id`, since the repository is wherever the user drops the
file.

### Quick create: name only

A new app under a group that already resolves an apps domain (below) needs
exactly one input — its name. The Admin wizard seeds the run precisely like a
resolved managed-domain address (`domain`, `label` under that domain) and then
drives the operation through two steps with no further input:

1. **`github` auto-submits `{"skip": true}`** the moment the operation parks
   there with no repository configured. Deploys are set up afterward from the
   app's own page.
2. **`verify` auto-submits `{}`.** The final "is it serving?" probe needs no
   choice from the user — same self-completing model as the address step for
   a wildcard-covered domain.

So the whole run is: create → (address resolves against the apps domain,
possibly converging it) → github skip → verify → `complete`, entirely without
the operator answering anything after the name. The public address the app
lands on serves the [placeholder page](#the-release-less-placeholder-page)
below the moment its vhost is enabled — which is what lets the automatic
`verify` step succeed with nothing deployed yet.

### The apps domain

`mojo.apps.edge.services.webapp_apps_domain` names the one domain new apps in
a group can go live under with **zero per-app DNS work** — the same
`*.{domain}` wildcard invariant the address step already exploited for a
single managed domain, generalized into a resolvable, convergeable concept of
its own.

- **`resolve(group)`** — the writable (`active`, `verified`, non-`mojo`
  provider) domain owned by `group` or any of its ancestors
  (`owned_by_group_or_ancestor`, the same ancestor walk
  `validators._group_at_or_below` uses — ancestors only, never siblings or
  another branch's descendants). When more than one domain qualifies, the one the
  platform's own `BASE_URL` hostname sits under (or exactly matches) wins —
  the longest suffix match if several nest; with no `BASE_URL` suffix match,
  a single remaining candidate is used, and more than one is ambiguous
  (`resolve` returns `None`, `no_domain_reason` reports which case it was).
  `installation_domain()` is the group-less flavor readiness uses: across
  every domain in the installation, same preference rule.
- **`converge(domain)`** — makes the domain's wildcard coverage exist:
  `status(domain)` checks for a `*.{domain}` CNAME at
  `webapp_destination.resolve()`'s target and an active-or-in-flight
  certificate covering a one-label probe host; `converge` writes whichever is
  missing (`dns.upsert_record` for the CNAME, `certs.request_certificate` for
  the apex-plus-wildcard cert) and does nothing when both already hold.
  Idempotent by construction — a second call finds everything in place.

**Actuation.** The address-advance step calls `converge(domain)` in place of
its ordinary per-host CNAME write whenever the resolved onboarding domain
**is** the group's apps domain — the first app onboarded under it pays the
one-time convergence cost and every later app reuses the wildcard. Setup's
readiness row (`apps_domain`, order 45 — see below) is the other caller: it
only *reports* pass/pending/fail, but its `pending` copy names the same lazy
path ("onboard a web app under this domain, or converge it from Domains") as
the backstop for an operator who wants the domain ready before anyone
onboards.

`owned_by_group_or_ancestor` also replaced the flat
`Domain.objects.filter(group=group)` scan in `precheck` and in the
address-choice `domain` validation, so a child group's guided address can
resolve against, and explicitly choose, an ancestor's domain — not only its
own.

### Apps-domain readiness

System Setup's `apps_domain` section (order 45, registered alongside the other
hosting sections — see
[Hosting readiness sections](../account/system_setup.md#hosting-readiness-sections))
reports the installation-level answer: `pass` with the domain and destination
once `installation_domain()` resolves one whose wildcard CNAME and certificate
both already exist; `pending` when no domain qualifies yet, or when one does
but convergence hasn't run (naming which of the record/certificate is
missing, and that onboarding the first app under it — or a manual converge
from Domains — creates it); `fail` only when reading the domain's DNS records
itself errors (a credential problem to fix, not a pending state). There is no
generic fixer button, matching the other hosting sections — convergence is
either lazy (onboarding) or an explicit Domains action.

Final verification makes one DNS-pinned HTTPS request to exactly `/`. Every
resolved address must be globally routable, SNI and `Host` retain the owned
hostname, redirects are not followed, and timeout/body size are bounded.

The frozen item-1818 handoff is
`GET /api/edge/webapp/summary?webapp=<id>`, `schema_version: 1`. It is
group-scoped and secret-free. Existing v1 meanings cannot change — but v1 is
**additive**: item 2099 added `address.domain` (id/name/provider; this also
fixed the portal's dead "Open domain" link), top-level `current_release`, and
`latest_deployment`, so a management view can answer "is my app live and serving
X?" in one call; item 2158 added `address.certificate` (`status`, `not_after`;
null only when there is no vhost — `Vhost.certificate` is a non-null FK) so the
same view answers "is SSL healthy?"; item 2230 added
`current_release.source`, so the same view says HOW that build got here.

For lists there is `GET /api/edge/webapp/summaries` (no parameters; human-only,
key-backed sessions refused): a bounded slim projection — one item per app the
caller may list, each a strict subset of summary v1 (`webapp` identity with
`deployment_ref`, `address` with `certificate`, `current_release` with
`source`, `latest_deployment` with its own `release` `{id, version, source}`) —
in a `{schema_version: 1, items, count, limit: 50, truncated, fleet}` envelope,
ordered by slug. It costs a flat two queries plus the count (select_related
rows and one Postgres `DISTINCT ON` latest-deployment batch) where per-app
`summary_for()` would be ~4 queries each. Visibility is exactly the REST
list's: the `VIEW_PERMS` global/group branches **and** the unconditional
`request.group` intersection, so a caller-supplied `?group=` confines rather
than widens. The merged Admin Deployments lane is its consumer; onboarding,
key, and probe facts stay in the per-app summary.

**`latest_deployment.release` is not `current_release`, and that is the point.**
After a rollback the newest deployment carries the release that failed while
`current_release` names the one that came back. A failure banner reads the
first; "what is still serving" reads the second. Collapsing them would make one
of those two sentences a lie exactly when it matters.

**The `fleet` block is scoped to the LISTED apps**, never to the installation:

```
fleet: {
  live,               # listed apps with BOTH an address and a current release
  domains,            # sorted distinct domain names of the listed primaries
  certificate_count,  # distinct certificates behind those primaries
  certificate,        # {wildcard, common_name, not_after, renew_after} or null
}
```

Two properties are deliberate. It is computed from rows `select_related`
already loaded, so it adds **no query** and cannot drift from the items beside
it. And `certificate` is **all-or-nothing**: it is populated only when exactly
one certificate backs *every* listed address, because naming one of several
would be a claim about apps it does not cover. `wildcard` is decided here — the
server scans `common_name` plus `sans` for `*.<domain>` — rather than leaving
two surfaces to guess that a wildcard can be carried by either. A truncated
list is not looking at every app, so its consumer drops the domain and
certificate claims. `schema_version` stays **1**: all of this is additive.

### First-deploy bootstrap

The WebApp admin UI cannot mint its own key before its first deployment. Run
the management command against the already-deployed Django platform instead.
For an existing WebApp (for example MojoVerify Portal, id 1), pipe the token
straight into GitHub so it never lands in shell history or a file:

```bash
ssh api-host '/opt/api/.venv/bin/python /opt/api/manage.py webapp_bootstrap \
  --webapp 1 --token-only' \
  | gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_WEBAPP

gh variable set MOJO_API_URL --body 'https://api.example.com' \
  --repo YOUR_ORG/YOUR_WEBAPP
gh variable set MOJO_WEBAPP_ID --body '1' \
  --repo YOUR_ORG/YOUR_WEBAPP
```

If the WebApp row does not exist yet, the vhost must already exist and the
release bucket must be in `EDGE_RELEASE_BUCKETS`:

```bash
python manage.py webapp_bootstrap \
  --group 123 --slug portal --vhost 456 --bucket edge-releases --token-only
```

Pipe that stdout into `gh secret set` in the same way. The command writes the
created `MOJO_WEBAPP_ID` to stderr so it remains visible while stdout carries
only the token. It refuses to replace an existing key unless `--rotate` is
explicit. Normal later rotation belongs in **Admin → Deployments → Manage
key** in the built-in Admin portal.

The onboarding flow supersedes this manual bootstrap for new sites. The command
remains for recovery and pre-Admin installations.

## Node-side

The desired-state payload gains a `webapps` key — **no second endpoint** — and
the generation hash covers it, so a promote moves the hash and nodes reinstall.
One row per serving vhost: the app's primary, plus one per
[alias address](#extra-addresses-aliases) carrying the identical release.

```
/opt/www/<vhost-id>/releases/<version>/           retained across generations
EDGE_ROOT/generations/<gen>/www/<vhost-id> ->     the pointer, INSIDE the generation
```

The pointer living inside the generation is what makes the pair coherent: one
`os.replace` of `current` swaps configuration and content together, and a
rollback reverts both. An earlier design put a `current` symlink next to the
release, outside the atomic swap — so a failed `nginx -t` abandoned the config
change but left the content already moved, and the node served a new bundle
under old config.

### The release-less placeholder page

A vhost with no release row (`stage_web_roots`'s `row is None` branch) no
longer gets an empty directory that answers 404. It gets a real directory —
inside the generation, so the same atomic swap replaces it once a release
exists — holding one static `index.html` written by `installer._placeholder_page`:
fully self-contained (inline styles, no scripts, no external assets), whose
only dynamic value (the vhost's `server_name`) is `html.escape`d, never
trusted as markup. It tells the visitor HTTPS works and nothing is deployed
yet.

This is load-bearing for [quick create](#quick-create-name-only): a brand-new
app's address is live and answers `200` on `/` the instant its vhost is
enabled, which is what lets the onboarding run's automatic `verify` step
succeed with zero deploys. It is also what the Admin portal's app list and
detail page read as "live with a welcome page — nothing deployed yet",
distinct from "not reachable" (no vhost at all).

### The app fetches the bytes

`services/www_sync.py` runs **inside `install()`**, before anything is staged,
so a promote lands on the fleet with no operator action. Per file: skip it when
the on-disk sha256 already matches the manifest, otherwise download to a
`.wwwsync-*` temp file in the destination directory, **re-hash it**, and only
then `chmod 0644` + `os.replace` it into place. The manifest hash is the only
thing trusted — a swapped bucket, an overwritten key or a truncated transfer
all fail identically, leaving the previous file untouched.

That skip is what makes it cheap enough to run every ten minutes: a converge
that changes no release does a stat and a hash per file and no S3 call at all.
`EDGE_RELEASE_FETCH_BUDGET` bounds one release's fetch; past it the remaining
files are left for the next converge, which resumes by hash.

**A release that will not fetch degrades ONE vhost:**

| Situation | What the node does |
|---|---|
| The vhost was already serving | Keeps serving **the exact release it served before** — the new generation's `www/<id>` is linked at `current`'s old target, not at the promoted version. |
| The vhost never served anything | Excluded from the generation (dark). A live vhost pointing at nothing is worse. |
| Either | `installed.json` records `www_pending: {<vhost id>: <version>}`, one incident is reported **the first time**, and every later converge retries silently. |

`www_pending` **defeats the generation short-circuit**: a degraded node
re-installs the same generation every converge until the fetch succeeds, at
which point the web root re-points itself. A healthy node still returns
`unchanged` and does zero S3 work. Cert exclusions are unaffected — this is
fetch-only.

`stage_web_roots` still refuses a release directory that is not on disk. That
check is now the safety net *behind* the fetch (a hand-deleted release, a disk
that filled mid-install), not the fetch's error path — reaching it means
something removed a directory `www_sync` had already verified.

A rollback to a release still on disk remains a pure symlink flip, no
re-download; `prune_releases` will not delete a release any retained
generation still references, which is what keeps that true.

### Credentials for the fetch

**Ambient, and deliberately not the platform's.** `www_sync.get_s3_client`
passes no access key, so boto3's default chain resolves — on a node that is the
instance role. The grant it needs is exactly:

```
s3:GetObject on arn:aws:s3:::<release bucket>/webapps/*
```

No `ListBucket`: every key comes from the manifest, so a node never enumerates.
Never the platform `S3Config` rows or `AWS_KEY`/`AWS_SECRET` — those sign
uploads, and a node only reads. **Do not export platform AWS keys into the
runner's environment**: botocore's default chain prefers environment variables
over the instance role, so an ambient key silently widens what a node's fetch
could reach.

`EDGE_RELEASE_BUCKETS` is DB-backed (`settings.get`, so a `Setting` row can
change it) **by design** — parity with the upload-signing path, which resolves
it the same way. It is re-checked here at fetch time, so a bucket removed from
the allowlist stops being read as well as written; and the bound that survives
a moved allowlist is the manifest hash, which is stored on the release row and
never comes from the bucket.

## A site served elsewhere

`WebApp.vhost` is nullable. Such a site is registerable, promotable and
rollbackable, and is simply **absent from the desired-state payload** — nodes
never hear about it. If CloudFront fronting is adopted it needs no model
change; a delivery-mode enum would be a speculative second code path.

## Extra addresses (aliases)

An app answers on exactly **one primary address** — `WebApp.vhost`, the one
onboarding mints and the one deploy, health, summary and take-offline already
reason about. An **alias** is one more `Vhost` for the same app, marked by
`Vhost.alias_of` (nullable FK to `WebApp`, `on_delete=CASCADE`, migration
`0013_vhost_alias_of`). `www.customer.com` and the platform address then serve
the identical release from the identical bytes.

**The primary never moves.** Attaching an alias does not repoint
`WebApp.vhost`; changing the address is still the onboarding change-address
flow. That is what keeps every existing surface's meaning intact — one added
column, no second code path.

**`alias_of` is in `Vhost.RestMeta.NO_SAVE_FIELDS`.** Only
`services/webapp_alias.py` sets it. A settable field would let a `manage_dns`
holder graft any vhost they can see onto any WebApp they can see and skip
domain ownership, conflict, DNS and certificate checks in one POST.

### Invariants (`validators.validate_vhost_alias`)

Called from `validate_vhost`, so `save()` enforces them on every path — no
service can skip them, and a hand-written row cannot bypass them either:

| Invariant | Why |
|---|---|
| `kind` is `site` or `site_api` | An alias serves an app, so it is never a redirect or a whole-host proxy. |
| Alias **XOR** primary | A row that is both `WebApp.vhost` and someone's `alias_of` would be torn down twice and rendered under two owners. |
| Domain owned by the app's group **or an ancestor** | The same rule `validate_web_app` applies to the primary. A **house** domain (`group_id` null) is nobody's ancestor, so the platform's own names stay unreachable — the identical finding the primary check was written for. |
| `pool` matches the primary's | A different pool is a node fleet that installs the alias's server block but never the release bytes behind it. |

### `attach()` is a re-enterable status machine

`webapp_alias.attach(web_app, hostname, actor, retry_certificate=False)` is the
whole flow. The UI's **Check** button is this same call, so it makes at most
one provider write per call and none at all for an address that is already
attached. A state the user can fix comes back as a status, never an exception:

| Status | Meaning |
|---|---|
| `needs_domain` | No active, verified `Domain` here covers that hostname. Connect it on the Domains page first. |
| `records_needed` | External DNS (`provider == "mojo"`) and the CNAME is not published yet. Carries `records` — `{type, name, value, ttl}` — including the `_acme-challenge` record when the delegation is missing or `broken`. |
| `certificate_pending` | Issuance is in flight (an existing `pending`/`issuing` row, or one this call requested). |
| `certificate_failed` | The domain's newest apex+wildcard certificate is `failed`. Carries repair `records`; only an explicit retry re-requests. |
| `attached` | Live. `created` is `False` when it already was — that is what makes Check free to press. |

Every result also carries `hostname`, `reason` (plain language, rendered
verbatim by the UI) and `dns`: **`managed`** when the platform writes this
zone's records, **`external`** when the customer does. `dns` is derived from
`Domain.provider` — `mojo` means certificate-only delegated ACME, so it is the
`external` case.

**It never initiates a delegation.** There is no public-suffix list in this
repo, so "the registrable parent of `shop.customer.co.uk`" is a guess, and
`delegation.initiate` on a guessed name delegates ACME control of the wrong
zone. `needs_domain` steers to the Domains page instead; connecting a domain
stays its own deliberate flow.

**A failed certificate is re-requested only on explicit retry.**
`certs.request_certificate` reuses a row only while it is pending, issuing or
active — a `failed` row matches nothing, so an auto-retrying Check would mint a
brand-new ACME order on every click and burn the rate limit. `attach()` looks
up the newest certificate matching the domain's apex+wildcard name set itself
and returns `certificate_failed` until the caller passes `retry_certificate`.

Four things are **refusals**, not statuses, because retrying will never help:

- a wildcard (`*.…`) — one exact address only;
- the bare apex — `www.<domain>` is suggested instead;
- a deeper label (`a.b.example.com`). One label keeps every alias inside the
  apex+wildcard certificate the domain already has; a deeper name would need
  its own certificate;
- **any** foreign enabled vhost at that name — another app's primary, another
  app's alias, or an admin-created vhost of any kind. Refusing plainly is what
  keeps the enabled-name uniqueness constraint from surfacing as an
  `IntegrityError`;
- managed DNS where the name already carries other records, or a CNAME pointing
  somewhere else. (A `*.{domain}` CNAME already at the destination routes this
  name too, so no per-host record is written.)

**Ancestor domains need authority in the owning group.** Reading an ancestor's
domain is the inheritance contract; *writing* to it (the CNAME, a certificate
request) requires `webapp_authority.can_manage_group_webapps(actor,
domain.group)` — checked before any provider read or write.

On success the alias vhost is created as `kind="site_api"`, `spa=True`, the
primary's `pool`, and the covering certificate, then `_reconcile_routes()`
copies the primary's **complete route contract**. Hosted-auth is first
reconciled on the primary; its proven upstream is passed explicitly to the
alias, and every application route copies its upstream FK from the primary
row. Re-checking is idempotent. A duplicate logical path, an alias-only path,
or the same path pointing at another upstream is refused atomically rather
than guessed at or silently rewritten.

`detach(web_app, vhost)` removes one alias — `Vhost.delete()` publishes fleet
convergence on commit, so nodes drop the server block without waiting for the
sweep. The certificate and the app are untouched, and the app's own address is
refused (taking the site offline is the deliberately louder `detach_address`).

`status_rows(web_app)` returns the primary first (`role: "primary"`) then
aliases by pk, each with `vhost`, `hostname`, `domain` (`{id, name,
provider}`), `dns`, `enabled` and certificate `status`/`not_after`. No provider
round trip — a status list must not spend one per row.

`webapp_onboarding.summary_for` gained an additive `address.aliases` list
(hostname + certificate per alias). `schema_version` stays **1**: v1 is
additive-only, and the list is `[]`, never null, so a reader never branches on
absence.

### The pre-write gate, and asking without doing

Every check `attach()` clears **before** it touches a provider or the database
lives in `_resolve_target(web_app, hostname, actor)`, in the order it always
ran: address-first precondition → wildcard refusal →
`naming.normalize_domain` → `_domain_for` → apex refusal → deep-label refusal →
`validators.validate_label` → ancestor-group write authority. It returns
`objict(domain, label, hostname)` with the hostname normalized, and `domain`
`None` when nothing here covers the name. `attach()` calls it and carries on;
the `needs_domain` sentence itself is `_needs_domain_reason(hostname)`, so both
callers say one thing about one fact.

`preview(web_app, hostname, actor)` is that gate and **nothing else**:

| Status | Carries |
|---|---|
| `ready` | `hostname`, `dns` (`managed` / `external`), `domain` (`{id, name}`) |
| `needs_domain` | `hostname`, the same `reason` `attach()` returns |
| `unusable` | the raw lowered/stripped `hostname`, and `reason` = the sentence `attach()` would have raised |

Three properties are load-bearing:

- **It is free.** No `dns.list_records`, no `dns.upsert_record`, no
  `certs.request_certificate`, no `probe.query_cname`, no certificate lookup,
  no write. The dialog calls it while someone is typing, so one provider round
  trip here is a round trip per keystroke.
- **It reports no occupancy.** "That address is already serving something else"
  is a fact about another tenant's vhost; a keystroke-fast endpoint that
  answered it would be a probe for which names are taken. The write still
  refuses, plainly, at submit.
- **Only `ValueException` is caught.** `PermissionDeniedException` from the
  ancestor-authority check **propagates**, so the preview refuses exactly as the
  write does and the security incident still fires. Turning it into a 200
  `unusable` would launder a denial into a hint.

### Deploy fan-out

`releases.desired_webapps` emits **one row per alias carrying the identical
release** as the primary, keyed by the alias's own vhost id (that, not the
slug, is what a node turns into a filesystem path). Rows are sorted by vhost id
so the payload stays stable.

Alias rows are emitted **only while the app still has a primary**
(`alias_of__vhost__isnull=False`). Taking a site offline detaches the primary;
an alias row surviving that would leave the customer's own domain serving
content the operator just took down.

The **primary keeps its hard proof** in `webapp_deploy.install_node`. A lagging
alias — bytes still in `www_pending`, or a vhost excluded from this generation —
is a **named warning** on that node's job result
(`metadata.webapp_deployment.alias_warnings`, visible through `target_status`)
and a log line. Failing the node instead would roll the whole release back, and
because the identical check runs during **rollback**, one customer domain's
transient problem would terminally fail every deploy for that app. The warnings
are deliberately **not** written onto `WebAppDeployment.detail`: several nodes
write concurrently, and `orchestrate` overwrites that field with the terminal
outcome anyway.

`webapp_auth_routes.owning_app(vhost)` resolves the owning app through *either*
link — the `web_app` reverse of `WebApp.vhost`, or `alias_of`. `reconcile_all`
now iterates serving **vhosts** rather than apps, and `rendered_contract` uses
`owning_app`, so an alias serves the identical auth routes and honeypots. An
alias without them would serve the SPA with no `/auth` route: logging in on the
custom domain would 404 while the platform address worked.

Attach also reconciles the primary's application routes onto the alias. Auth
routes are not re-derived independently on each hostname: the primary proves
the hosted-auth upstream once, and the alias receives that exact destination.
That keeps custom domains and the platform address on one route contract even
when configuration discovery would otherwise be ambiguous.

### Teardown

Both teardown paths delete alias vhosts **in the same transaction** as the
thing that owns them, one explicit `Vhost.delete()` each:

- `WebApp.on_rest_delete` — `alias_of` cascades, but a bare cascade deletes the
  *rows* without running `Vhost.delete()`, so nodes would keep serving every
  custom domain until the next sweep.
- `edge/webapp/detach_address` — "offline" that left the customer's own domain
  serving is the opposite of what was asked. It deletes both `site` and
  `site_api` primaries; a malformed link to another vhost kind, or an invalid
  alias kind, is refused before anything is unlinked or deleted. The app's
  object permission check and exact `alias_of=web_app` query keep unrelated
  tenants and vhosts outside the transaction. Before the primary is deleted,
  its non-managed routes are captured as `WebAppRoute` desired state. A later
  address restore rematerializes them; managed auth routes remain derived.

Onboarding's address step also refuses an existing vhost that is an alias —
including **this** app's own alias. Adopting it as the primary would leave one
vhost owned twice, which `validate_vhost` refuses outright, so the refusal is
raised plainly where the operator can see it.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `EDGE_RELEASE_BUCKETS` | — | **Fails closed.** No declared buckets, no sites — and no fetches. DB-backed, both when signing an upload and when a node reads. |
| `EDGE_RELEASE_MAX_FILES` | `5000` | Manifest entry cap |
| `EDGE_RELEASE_MAX_BYTES` | `1073741824` | Manifest total-size cap. A count cap does not bound bytes, and every node fetches the release onto its own disk |
| `EDGE_RELEASE_UPLOAD_TTL` | `3600` | Presigned PUT lifetime, seconds |
| `EDGE_RELEASE_FETCH_TIMEOUT` | `60` | Per-attempt connect/read timeout for a node's S3 GET (static) |
| `EDGE_RELEASE_FETCH_BUDGET` | `300` | Wall-clock ceiling for one release's fetch; the remainder resumes next converge (static) |
| `EDGE_KEEP_RELEASES` | `5` | Retained releases per vhost, enforced by `www_sync.prune_releases` (static) |

`EDGE_KEEP_RELEASES` counts the promoted release, so it bounds how many
*extra* releases stay on disk for a quick rollback. It never overrides the two
exemptions — the desired version and anything a retained generation still
symlinks — so setting it to `1` (or `0`) cannot delete what is being served.
Pair it with `EDGE_KEEP_GENERATIONS`: a generation retained for rollback is
useless if the release it points at was pruned, so keeping N generations means
wanting at least N releases.

## Day-2 management

After a site is live, these endpoints manage it. All are group-scoped through
`webapp__group`, and the pk-fetching custom actions carry an explicit
`rest_check_permission_or_raise` (a `requires_perms`/`uses_model_security`
decorator gates the verb, never the specific row).

| Endpoint | Method | What it does |
|---|---|---|
| `edge/deployment`, `edge/deployment/<pk>` | GET | Fleet-convergence history. Filter a list with `?webapp=<id>`. Read-only; rows are made by promote/rollback only, and a cross-tenant id is not readable. |
| `edge/webapp/rollback` | POST | Repoint a site at an already-verified earlier release. **Human-only**: `denies_key_backed_session` keeps CI keys out, so "deployment starts only from release completion" still holds for automation. A foreign release id 404s; a `pending` release is refused. Returns `webapp_deploy.payload(...)`. |
| `edge/webapp/health` | GET | On-demand public HTTPS reachability of the live address: `healthy` / `unhealthy` / `not_configured` (no vhost). Never echoes a raw probe exception. |
| `edge/webapp/detach_address` | POST | Take a site offline. Body `{webapp}`; returns `{webapp, address: null}` after retaining its custom route intent, then unlinking and deleting its `site` or `site_api` serving vhost while keeping the app. Every alias address goes with it. Restoring an address rematerializes the retained routes. Malformed non-site ownership is refused before any teardown. Human-only, fresh-auth, `manage_webapp`, plus the explicit object check. |
| `edge/webapp/attach_domain` | POST | Point one more address (an **[alias](#extra-addresses-aliases)**) at this site. Body `{webapp, hostname, retry_certificate?}`; returns `webapp_alias.attach()` plus `webapp` — `status` (`needs_domain` / `records_needed` / `certificate_pending` / `certificate_failed` / `attached`), a plain `reason`, `dns` (`managed` / `external`) and, when the caller has records to publish, `records`. Re-enterable: the UI's Check button is this same call, and an already-attached address makes no provider write. `retry_certificate` goes through `_flag()`, not `bool()` — a browser sends form values as strings and `bool("false")` is True, so only a real `True` or `"1"`/`"true"`/`"yes"`/`"on"` counts; everything else, junk included, is False. Human-only, fresh-auth, plus the explicit object check. |
| `edge/webapp/attach_preview` | GET | What `attach_domain` **would** do with `{webapp, hostname}`, without doing any of it — `webapp_alias.preview()` plus `webapp`: `status` (`ready` / `needs_domain` / `unusable`), `hostname`, and for `ready` the `dns` mode and `domain` (`{id, name}`). Deliberately **not** this file's read idiom: it answers a question about a write, so it carries the write's `manage_webapp` and the same explicit object check — but **no** step-up, because it changes nothing. Its only client is the manager-gated add-an-address dialog, which calls it as you type. Reports no occupancy, and an ancestor-authority denial propagates as a denial rather than a verdict. |
| `edge/webapp/detach_domain` | POST | Remove one alias address. Body `{webapp, vhost}`; the address is scoped to this app's **aliases**, so the app's own address and a foreign one both 404 rather than disclose. The certificate and the app are untouched. Human-only, fresh-auth, plus the explicit object check. |
| `edge/webapp/aliases` | GET | `webapp_alias.status_rows()`: every address this app answers on, primary first (`role: "primary"`), then aliases — `vhost`, `hostname`, `domain` (`{id, name, provider}`), `dns` mode, `enabled`, and certificate `status`/`not_after`. Human-only, but a read — so no step-up, `VIEW_PERMS`, and no provider round trip. |
| `edge/webapp/serving` | GET | Everything about how this app is served, as one payload — see [Serving](#serving-address-certificate-shape-and-paths). A read: `VIEW_PERMS`, no step-up. `serving.pools` and `upstreams` are populated **only** when the caller also passes `SAVE_PERMS` (evaluated non-raisingly with `rest_check_permission`); a viewer gets `null` for both. |
| `edge/webapp/serving` | POST | Change `pool`, `spa` and/or `certificate`. Everything else in the body is ignored — `kind` in particular, because the serving shape decides which fields the renderer has a branch for at all. Applies to the primary **and every alias**, primary first. Human-only, fresh-auth, plus the explicit object check. Returns the same payload as the GET, with editables. |
| `edge/webapp/certificate` | POST | Request a certificate covering this app's address **alone**. Requesting only — switching is a separate `serving` save. Body `{webapp}`. Human-only, fresh-auth, plus the explicit object check *and* the domain-owning-group authority check below. |
| `edge/webapp/add_route` | POST | Retain one custom path as `WebAppRoute` desired state and send it to a declared destination on the primary and every alias. Body `{webapp, path_prefix, upstream}`. Refuses a [managed prefix](#managed-prefixes-are-derived), a prefix already pointing elsewhere, a destination outside `webapp_auth_routes._accessible_upstreams`, and any app whose primary is not `site_api`. `/path` and a legacy `/path/` row are one identity: one legacy row heals to canonical; both spellings together are refused as ambiguous. |
| `edge/webapp/remove_route` | POST | Remove one retained custom path and stop sending it elsewhere on the primary and every alias. Body `{webapp, path_prefix}`. Refuses a managed prefix, a prefix that is not set up, and an ambiguous canonical-plus-trailing-slash duplicate. Either single spelling is removed. |
| `DELETE edge/webapp/<pk>` | DELETE | **Safe delete.** `WebApp.on_rest_delete` runs the teardown — deactivate + unlink the `MOJO_DEPLOY_KEY` credential, delete the serving vhost and every [alias](#extra-addresses-aliases) vhost — in the **same** transaction as the row delete. The framework's `on_rest_pre_delete` hook runs *outside* the delete transaction, so it is the wrong hook: teardown there could commit while the row delete fails. A bare cascade orphaned the vhost and left the CI key active. Release bytes in S3 are intentionally left. |

`rollback` is `releases.promote` with an earlier release — the same idempotent,
supersede-safe primitive as forward promotion, under a `select_for_update` row
lock. A deployment row cascade-deleted with its WebApp (safe-delete racing an
in-flight deploy) makes `webapp_deploy.orchestrate` a superseded no-op rather
than a crashing `DoesNotExist`.

The two destructive transactions above — take-offline and safe-delete — live in
`services/webapp_lifecycle.py` (`take_offline`, `teardown`). The REST detach
handler and `WebApp.on_rest_delete` are thin callers, and so is the Admin
Assistant, which reaches this same day-2 surface through its `webapp` tool
domain (`docs/django_developer/assistant/webapp_tools.md`). Chat gets the same
services under the same authority and the same fresh-auth windows; minting or
rotating `MOJO_DEPLOY_KEY`, buying a domain, and uploading a build are not
available there and stay portal-only.

### Serving: address, certificate, shape, and paths

`services/webapp_serving.py` is the whole domain layer behind the five
endpoints above. It answers one question — how is **this app** served — and it
is expressed in terms of the app, never of a vhost.

`serving_for(web_app, include_editables=False)` returns one payload:

```
{schema_version: 1,
 webapp:      {id, slug, display_name},
 address:     {vhost, hostname, https_origin, domain: {id, name, provider},
               dns, wildcard: {covered, name}},
 certificate: {id, common_name, sans, status, not_after, renew_after,
               days_remaining, wildcard, dedicated: {id, status, ready}|null,
               dedicated_supported, dedicated_reason},
 serving:     {kind, pool, pools, spa, routes_supported},
 routes:      [{id, path_prefix, upstream: {id, name}, managed}],
 upstreams:   [{id, name}],
 aliases:     [{vhost, hostname}]}
```

An app with no address returns the same shape with nulls, not an error.

#### Managed prefixes are derived

A route is `managed: true` when its `path_prefix` is in
`webapp_serving.managed_prefixes()` — `tuple(webapp_auth_routes.auth_route_prefixes())`,
the resolved hosted-auth contract (`/auth`, `/register`, `/passkey` from the
`BOUNCER_*` settings, plus `/api/auth`, `/api/account`, `/api/login`,
`/api/refresh_token`).

There is **no model field and no migration**. A stored flag would let the two
disagree the moment a bouncer path setting changed, and the stored answer would
win — an app would show `/auth` as editable while the renderer still owned it,
or show a stale prefix as managed after the setting moved. The writes refuse
these prefixes outright, so there is nothing to keep in step.

#### Every serving write applies to every address, primary first

`apply`, `add_route` and `remove_route` each touch `WebApp.vhost` **and** every
`Vhost.objects.filter(alias_of=web_app)` in one `transaction.atomic()`.

The order is not cosmetic. `validators.validate_vhost_alias` re-reads the
primary's pool on **every** alias save and refuses an alias whose pool differs,
so saving an alias into the new pool before the primary moved is refused by the
app's own invariant. Primary first, always.

Each `Vhost.save()` publishes convergence for the pool it left and the pool it
joined (`convergence.publish_after_commit(previous_pool, self.pool)`), so a
pool move republishes both fleets without a bespoke path.

Route identity is canonical without abandoning legacy rows: `/reports` and a
stored `/reports/` mean the same path. `add_route` heals one safe legacy row to
`/reports`; `remove_route` finds and removes either spelling. If both rows
exist, the service refuses the ambiguous identity before touching any address.
The same canonicalization and duplicate refusal runs while copying a primary's
route contract to a new or existing alias.

Custom route intent lives in `WebAppRoute`; `VhostRoute` is its per-address
materialization. `add_route` and `remove_route` update both layers in the same
transaction. `take_offline` first captures the primary's current non-managed
rows, which is the upgrade bridge for apps created before the desired-state
table existed, then deletes the primary and aliases. Address onboarding
validates every retained upstream against the selected domain before any DNS
write and recreates the custom rows after hosted-auth reconciliation. A route
that would cross tenants or use a retired destination is refused visibly; it
is never silently omitted.

Two refusals `apply` raises before it writes anything:

- an **undeclared pool** (`validators.validate_pool`), refused before the
  transaction so nothing publishes for a pool no node serves;
- an **SPA toggle on a `kind="site"` vhost carrying a `mojosec_policy`**. That
  policy pins the expected `response_class` to `spa_fallback` or `static_site`
  depending on `spa`, so flipping it would raise a renderer-contract error deep
  inside `validate_vhost`. `apply` says the plain thing instead: *"This app has
  a security policy tied to its current mode — update the policy first, then
  change this."*

`certificate` is **primary-only** — an alias serves a different name and holds
whatever certificate covers it — and must belong to the primary's domain, be
`active`, and pass `validate_certificate_covers`.

#### The dedicated certificate is two phases, and actor-gated

`dedicated_supported` is `delegation.for_domain(domain) is None and
domain.provider != PROVIDER_MOJO`. Both excluded cases route through
`certs._require_delegated_profile`, which allows **exactly** the apex plus
wildcard profile, so an exact-name order there is refused by the issuer.
`dedicated_reason` carries the plain sentence the UI renders in place of the
control.

`request_dedicated_certificate(web_app, actor)`:

1. **Authority.** When `domain.group_id != web_app.group_id`, require
   `webapp_authority.can_manage_group_webapps(actor, domain.group)` before any
   provider action — the same gate `webapp_alias.attach()` applies. Reading an
   ancestor's domain is the inheritance contract; an ACME order against that
   zone is a write, and needs authority in the group that owns it.
2. **Pre-scan.** Return an existing exact-name row in `pending`, `issuing` or
   `active` rather than calling the issuer. `certs.request_certificate` RAISES
   on an active row that is not due for renewal, so a second press on a
   finished request would report an error over a perfectly good certificate.
3. Only with no such row: `certs.request_certificate(domain, names=[hostname])`.

Switching the app onto it is a **separate** `apply(certificate=...)`, allowed
only once the row is active and really covers the name — a vhost pointed at a
pending certificate would serve nothing at all.

#### Fleet inventory is a writer's fact

`serving.pools` (`validators.declared_pools()`) and `upstreams`
(`webapp_auth_routes._accessible_upstreams`, enabled only) are the deployment's
own topology: which node pools exist, and which upstream names an ancestor org
declared. They are populated only when `include_editables` is true, which the
REST layer sets from a non-raising `SAVE_PERMS` check. A read-only viewer of
one app gets `pools: null, upstreams: null` and the same four cards with values
and no controls.

## Scope boundary

django-mojo **tracks and orchestrates**. It does not build, and it does not
proxy the upload — CI uploads to S3 directly and the API only registers what
landed. Keeping multi-megabyte bundles out of the request path is deliberate.

The node-side fetch is not an exception to that: it runs in the converge job on
the node that will serve the bytes, streaming S3 to disk, and never inside a
request.

## Deployment-key readiness

System Setup reports each WebApp's deployment-key lifecycle as missing, active,
rotated, inactive, or revoked using `WebAppKeyOperation` receipts and safe
`ApiKey` metadata. It never reads back a token: mint/rotate returns the token
once, and a replay returns only the receipt/current status. Losing that response
requires an explicit rotation. The readiness response contains no token hash,
encrypted token, or recoverable credential material.

## Serving-destination readiness

System Setup's `webapp_destination` section (order 44) runs the same
`webapp_destination.resolve()` used by onboarding and reports `pass` with the
resolved destination and its provenance, `pending` when nothing is configured
yet (an install that has simply not finished Setup, not a broken one), or
`fail` when `EDGE_WEBAPP_CNAME_TARGET` is set but not a usable hostname (a
misconfiguration to fix now). This is what lets an operator see, before
onboarding anyone, whether the installation can serve app addresses at all —
see [destination resolution](#url-first-entry-and-external-domains) above for
the resolver itself.
