# Releasing a site build

The CI-facing contract. If you are wiring a pipeline, this is the page.

Backend reference:
[django_developer/edge/webapps.md](../../django_developer/edge/webapps.md).

## What your pipeline needs

- A **site** (`WebApp`) already registered by an administrator.
- The site's **service key**, minted once with
  `POST /api/edge/webapp/link_key` and stored in GitHub Actions as the secret
  named exactly **`MOJO_DEPLOY_KEY`**. Authenticate with
  `Authorization: apikey <token>`.

Developers do not receive deploy keys. A merge or push to the configured branch
starts the repository's workflow; the WebApp-linked service key can release
only that one WebApp. Automatic deployment is the default. `auto_promote=False`
is reserved for an explicit manual-hold exception.

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

## Manual promotion and rollback (exceptional admin flow)

```
POST /api/edge/webapp/promote  {"webapp": 42, "release": 7}
```

Requires `manage_webapp`. It uses the same fleet coordinator as automatic
deployment. **Rollback is the same call** with an older release id.

Rolling back to something older is safe too. Nodes retain a bounded number of
releases, and a target that has aged out is simply **re-fetched from S3** on
the next converge — you never need to re-upload a build, and you never need to
know whether a given node still has one. Recent releases stay a pure symlink
flip; an older one costs a download before it goes live. The one thing that
does end a rollback is the bucket: a lifecycle rule that expires old release
objects expires your ability to roll back to them.

Read history from `GET /api/edge/release?webapp=42`. Statuses are `pending`,
`uploaded`, `live`, `superseded`.

## Key rotation

`POST /api/edge/webapp/link_key` again. The previous key is deactivated
**immediately** — there is no grace window, so update your pipeline's secret
before rotating, not after.

Revoking a key stops future releases and does **not** change what the site is
currently serving.
