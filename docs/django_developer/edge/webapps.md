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
  vhost            FK, nullable
  bucket           from EDGE_RELEASE_BUCKETS
  prefix           DERIVED: webapps/<group>/<id>
  api_key          the CI credential, one per site (OneToOne)
  current_release  what nodes should serve

WebAppRelease
  webapp, version (unique together), manifest, status, created_by
  status: pending -> uploaded -> live -> superseded

WebAppDeployment
  release, previous_release, status, targets, rollback_targets, detail
  status: queued -> deploying -> live
                       \-> rolling_back -> rolled_back / failed
```

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
the human control plane. Intentional rollback means rerunning the workflow for
an older commit; its immutable manifest is reused and the same deployment
coordinator converges it.

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

Admin → WebApps drives a resumable four-step operation: **App → Address →
Connect GitHub → Verify**. `WebAppOnboardingOperation` stores versioned intent,
provider identity, bounded evidence, a revision, and a short lease. It never
stores a registrar confirmation token, GitHub installation token, deployment
key, certificate material, or raw provider error. `WebApp`, `Domain`,
`Certificate`, and `Vhost` remain authoritative for their resources.
The model and the WebApp profile fields (`display_name`, `environment`,
`github_repository`, `deployment_ref`, and `build_output`) land together in
`edge.0009_webapp_onboarding`, after `edge.0008_webappkeyoperation` and the DNS
reservation dependency.

The operation is actor-, group-, and origin-bound. Every continuation rechecks
the active actor, group permission, object scope, and original origin. API keys
and group tokens are refused. Workers use atomic leases and bounded backoff;
after an uncertain provider result they read authoritative inventory before
writing again. Cancellation stops recovery but preserves proved resources.

Domain purchase still uses the registrar's live quote and typed domain/price
confirmation. The operation commits only a hash-bound purchase intent, then
the fresh-auth request consumes the raw one-use confirmation synchronously.
Workers never receive it. A lost response is recovered from `DomainPurchase`
and `Domain.metadata.purchase`, not by replaying money movement.

Guided DNS accepts exactly one concrete non-apex, non-wildcard label. It
inventories the complete record set, adopts an exact CNAME, and refuses mixed,
ambiguous, or foreign values.
The target is the file-only `EDGE_WEBAPP_CNAME_TARGET`. Certificate selection
reuses an active exact/wildcard certificate outside its renewal window; private
material never crosses the onboarding surface.

GitHub evidence uses only a `GitHubInstall` whose `group` exactly matches the
operation. Repository, ref, and build-output values are whitelist validated.
Evidence is honestly `verified`, explicit `attested`, or `unavailable`. The
generated workflow keeps inputs in quoted environment variables and references
`${{ secrets.MOJO_DEPLOY_KEY }}` without embedding a secret.

Final verification makes one DNS-pinned HTTPS request to exactly `/`. Every
resolved address must be globally routable, SNI and `Host` retain the owned
hostname, redirects are not followed, and timeout/body size are bounded.

The frozen item-1818 handoff is
`GET /api/edge/webapp/summary?webapp=<id>`, `schema_version: 1`. It is
group-scoped and secret-free. Existing v1 meanings cannot change.

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
explicit. Normal later rotation belongs in **Admin → WebApps → Manage key** in
the built-in Admin portal.

The onboarding flow supersedes this manual bootstrap for new sites. The command
remains for recovery and pre-Admin installations.

## Node-side

The desired-state payload gains a `webapps` key — **no second endpoint** — and
the generation hash covers it, so a promote moves the hash and nodes reinstall.

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
