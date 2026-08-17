# Releasing a site build

The CI-facing contract. If you are wiring a pipeline, this is the page.

Backend reference:
[django_developer/edge/webapps.md](../../django_developer/edge/webapps.md).

## What your pipeline needs

- A **site** (`WebApp`) already registered by an administrator.
- The site's **service key**, minted with an explicit `mint` action at
  `POST /api/edge/webapp/link_key` and stored in GitHub Actions as the secret
  named exactly **`MOJO_DEPLOY_KEY`**. Authenticate with
  `Authorization: apikey <token>`.

For the first deployment—before the WebApp admin UI is available—a platform
operator runs `python manage.py webapp_bootstrap --webapp <id> --token-only`
on the Django platform and pipes stdout directly to `gh secret set
MOJO_DEPLOY_KEY`. Later rotation is available from the built-in Admin portal.

Developers do not receive deploy keys. A merge or push to the configured branch
starts the repository's workflow; the WebApp-linked service key can release
only that one WebApp. Verified completion always starts deployment; there is no
separate promotion approval or manual hold.

Use the canonical copyable action at
[`examples/github/actions/deploy-webapp`](../../../examples/github/actions/deploy-webapp).
It is intentionally an example path, not django-mojo's own `.github/actions`
directory: application repositories reference the released framework action
and keep only their build-and-trigger workflow locally.

## The flow

### 1. Register the release and get upload URLs

```
POST /api/edge/release
{
  "webapp": 42,
  "version": "a1b2c3d",
  "manifest": [
    {"path": "index.html", "sha256": "<hex>", "size": 1024},
    {"path": "assets/app.js", "sha256": "<hex>", "size": 51200}
  ]
}
```

You declare the manifest **before** uploading. That is what lets the API mint
one URL per file rather than handing you credentials for a whole prefix.
Paths are relative and slash-delimited; each segment may use letters, digits,
`.`, `_`, `-`, `!`, and `~`. This includes Next.js static-export route and
Turbopack filenames without allowing absolute or traversing paths.

Response:

```json
{
  "release": 7,
  "version": "a1b2c3d",
  "status": "pending",
  "bucket": "...",
  "prefix": "webapps/3/42/releases/a1b2c3d",
  "uploads": [
    {"path": "index.html",
     "url": "https://...",
     "headers": {"x-amz-checksum-sha256": "<base64>"}}
  ]
}
```

### 2. PUT each file

Upload straight to the returned URL. **Send the `x-amz-checksum-sha256` header
exactly as given** — it is bound into the signature, so S3 rejects both a
missing header and a body that does not match it.

Each URL is good for one object and expires in an hour (default). Bytes never
pass through the API.

The URLs are SigV4-signed, and the checksum header is the **only** header the
signature covers. `Content-Type` in particular is not signed — whatever your
HTTP client adds on its own is fine.

```bash
curl -X PUT --upload-file dist/index.html \
  -H "x-amz-checksum-sha256: <the value from the response>" \
  "<the url from the response>"
```

### 3. Complete

```
POST /api/edge/release/complete
{"release": 7}
```

The API checks every declared object against what actually landed — presence,
size, and S3's stored checksum. It does **not** take your word for it: a job
that half-failed can still call this, so "I am done" is not evidence.

On success the response includes `deployment` and `deployment_status`. Poll:

```
GET /api/edge/release/deployment/<deployment>
```

until `status` is `live` or `terminal` is true. The response includes each
active runner's job status and bounded diagnostics. `rolled_back`, `failed`,
and `superseded` are terminal failures for the workflow.

On failure you get a 400 naming the paths that did not verify. The release
stays `pending` and is not promotable.

## Errors worth handling

| Situation | What to do |
|---|---|
| Existing identical version | Safe retry. The existing release is reused; a different manifest for that version is still refused. |
| 400 listing paths at `complete` | Some uploads did not land. Re-upload those objects and call `complete` again. |
| 400 "no stored checksum" | The PUT omitted the checksum header. Send it. |
| 404 on `webapp` | The key is not this site's key, or the site has no key linked. Both look identical on purpose. |
| Deployment `rolled_back` | At least one active node failed; the prior release was restored. Surface the runner diagnostics and fail CI. |

## Rollback

Two ways, both human-driven.

**From the admin portal**, `POST /api/edge/webapp/rollback` with `webapp` and an
earlier `release` id repoints the site immediately. It is **human-only** — a CI
key-backed session is refused (`403`) — so automation still cannot start a
deployment out of band; deployment from CI happens only through verified release
completion. A `release` from another site returns `404`, and a `pending`
(unverified) release is refused. The response is the deployment status payload
(`GET /api/edge/release/deployment/<id>` shape).

**By rerunning the GitHub workflow** for the older commit: its identical version
and manifest are reused, then normal verified completion deploys it through the
same fleet coordinator.

Nodes retain a bounded number of releases, and a target that has aged out is
simply **re-fetched from S3** on the next converge. Recent releases stay a pure
symlink flip; an older one costs a download before it goes live. The one thing
that ends rollback is the bucket: a lifecycle rule that expires old release
objects expires the ability to deploy that commit again.

Read release history from `GET /api/edge/release?webapp=42` (statuses `pending`,
`uploaded`, `live`, `superseded`) and deployment history from
`GET /api/edge/deployment?webapp=42`.

## Key rotation

Call `POST /api/edge/webapp/link_key` with an explicit action and a fresh UUID:

```json
{"webapp": 42, "action": "rotate", "operation_id": "8f581ec1-e70f-4b90-8147-461e0308887e"}
```

The previous key is deactivated **immediately** and the new token is returned
once. The server does not retain a recoverable raw copy. Retrying the same UUID
returns `replayed: true` and `token: null`; rotate with a new UUID if the first
response was lost. Capture the successful first response and immediately
replace the GitHub `MOJO_DEPLOY_KEY` secret. A run caught in the short cutover
window fails safely and can be rerun.

Use `POST /api/edge/webapp/revoke_key` with `webapp` and a new `operation_id`
to deactivate and unlink the credential. Both mutations require a recent
interactive login and refuse machine-credential sessions. Link/create/rotate
uses a 600-second freshness window; revoke uses 300 seconds.

Revoking a key stops future releases and does **not** change what the site is
currently serving.

## Onboarding and workflow handoff

The built-in Admin portal creates sites through App → Address → Connect GitHub
→ Verify. The browser may resume an operation with:

- `GET /api/edge/webapp/onboarding/options?group=<id>`
- `GET /api/edge/webapp/onboarding/options?group_intent=new`
- `POST /api/edge/webapp/onboarding/create`
- `GET /api/edge/webapp/onboarding/detail?operation=<uuid>`
- `POST /api/edge/webapp/onboarding/choose` with the current `revision`
- `POST /api/edge/webapp/onboarding/cancel`

All onboarding routes require an interactive User session and refuse API keys,
group tokens, and override-user key sessions. Existing-group authority is
superuser, `security`, or both `manage_webapp` and `manage_dns`, resolved
globally or through the selected effectively active Group. New-group intent
also requires global `manage_groups`/`groups` for a non-superuser. Literal
`permissions.admin`, Admin admission, and UI capability data do not substitute
for that authority. Detail is readable only by the original actor while the
same two-part Group authority and active ancestry remain current.
Choose and cancel add a 600-second fresh-auth check; they also require the
original request origin, and choose must match the returned `revision` and
current `step`. Revoking the actor's WebApp authority or disabling any Group
ancestor stops both browser continuation and worker recovery.

Create accepts:

```json
{
  "group": 9,
  "slug": "customer-portal",
  "display_name": "Customer portal",
  "environment": "production",
  "bucket": "edge-releases",
  "github_repository": "NativeMojo/customer-portal",
  "deployment_ref": "main",
  "build_output": "dist"
}
```

That concrete-group form remains compatible and `operation_id` stays optional.
To create the owning Group, send no `group` and use a client UUID:

```json
{
  "group_intent": "new",
  "operation_id": "8f581ec1-e70f-4b90-8147-461e0308887e",
  "slug": "customer-portal",
  "display_name": "Customer portal",
  "environment": "production",
  "bucket": "edge-releases",
  "deployment_ref": "main",
  "build_output": "dist"
}
```

The new-group response returns at `cursor: "address"`: Group, WebApp, receipt,
and the WebApp's derived storage prefix committed together. Invalid input,
authorization failure, or storage failure creates none of them. Replaying the
same UUID/profile/actor/origin/intent returns the one receipt; changing any
binding is refused. Persist that UUID and nonsecret draft until
success/replay/cancel, so a lost response never creates a second Group. Freeze
the exact submitted payload: after transport ambiguity, query `detail` for the
UUID first. If it exists, discard the draft and mount that authoritative
operation. If it does not, replay only the unchanged payload; edits require
explicit abandonment and a new UUID.
Cancelling after the initial commit preserves the deliberate Group+WebApp pair
for recovery rather than deleting user-owned resources.

It returns `created` and a serialized operation. Detail, choose, and cancel
return the same versioned shape: `schema_version`, `operation_id`, `group`
(`id`, `name`), `status`,
`cursor`, `revision`, safe profile/choices/evidence/activity, related resource
ids, bounded recovery timing, and timestamps. Internal reconciliation state,
leases, actor/origin bindings, raw provider errors, and secrets are omitted.
Post a choice as
`{"operation":"<uuid>","revision":3,"step":"address","choice":{...}}`.
Address accepts exactly one concrete non-apex, non-wildcard label. A purchase
choice additionally carries the one-use `confirm_token`, `confirm_domain`, and
`confirm_price`; the token is consumed synchronously and never appears in a
later operation response.

Do not automatically retry `choose`: a provider can accept a mutation and lose
the response. Reload `detail`; the server reconciles durable intent against
authoritative inventory. Purchase confirmation remains synchronous and
fresh-authenticated; the raw quote token is never stored or sent to a worker.

`POST /api/edge/webapp/onboarding/workflow` returns the validated workflow for
one WebApp. It can also mint or rotate `MOJO_DEPLOY_KEY` when supplied `action`
and a fresh `operation_id`; the token appears only on the first successful
response. A replay returns `delivery: secret_unavailable`, so rotate explicitly
if the response was lost. The endpoint requires 600-second fresh interactive
auth and the same two-part WebApp+DNS authority in that WebApp's effectively
active Group. Its request is `{"webapp":42}` for the secret-free workflow, or adds
`"action":"mint|rotate"` and `"operation_id":"<uuid>"` for a one-time key
receipt. The response contains `schema_version:1`, repository, filename, and
validated YAML, plus `deployment_key` only when a key action was requested.

`GET /api/edge/webapp/summary?webapp=<id>` is the secret-free, group-scoped v1
read model. It includes profile, public address, onboarding evidence, and
boolean key readiness—never credentials, certificate keys, or internal state.
It requires an interactive session plus that WebApp's ordinary object read or
write authority (`view_dns`, `manage_dns`, or `security`).
