# Releasing a site build

The CI-facing contract. If you are wiring a pipeline, this is the page.

Backend reference:
[django_developer/edge/webapps.md](../../django_developer/edge/webapps.md).

## What your pipeline needs

- A **site** (`WebApp`) already registered by an administrator.
- The site's **API key**, minted with `POST /api/edge/webapp/link_key` and
  handed to you once. Authenticate with `Authorization: apikey <token>`.

That key carries exactly one permission, `release_webapp`. **It can upload and
it can never promote.** If your pipeline needs a build to go live
automatically, ask an administrator to turn on `auto_promote` for that site —
it is a per-site setting, so a marketing site can go live on push while an
admin portal waits for a human, from the same pipeline.

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

On success the release becomes `uploaded`, and `promoted: true` comes back if
the site has `auto_promote` on.

On failure you get a 400 naming the paths that did not verify. The release
stays `pending` and is not promotable.

## Errors worth handling

| Situation | What to do |
|---|---|
| 400 "release ... already exists" | Versions are immutable. Use a new one — do not retry with the same id. |
| 400 listing paths at `complete` | Some uploads did not land. Re-upload those objects and call `complete` again. |
| 400 "no stored checksum" | The PUT omitted the checksum header. Send it. |
| 404 on `webapp` | The key is not this site's key, or the site has no key linked. Both look identical on purpose. |
| 403 on promote | Expected. CI cannot promote; that needs `manage_webapp`. |

## Promotion and rollback (admin UI, not CI)

```
POST /api/edge/webapp/promote  {"webapp": 42, "release": 7}
```

Requires `manage_webapp`. **Rollback is the same call** with an older release
id — there is no separate endpoint, and no re-upload: the previous build is
still on the nodes, so it is a symlink flip.

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
