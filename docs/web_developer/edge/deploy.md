# Fleet code deploy API

Two endpoints feed the same deployment queue. The node implementation is
documented in
[django_developer/edge/deploy.md](../../django_developer/edge/deploy.md).

## `POST /api/github/deploy/webhook`

The public GitHub push webhook requires a valid `X-Hub-Signature-256`, using
HMAC-SHA256 with `GITHUB_WEBHOOK_SECRET` over the exact JSON request body.
Unsigned or incorrectly signed requests return `403`.

| Push | Response |
|---|---|
| Deploy branch (`EDGE_DEPLOY_BRANCH`, default `main`) | `202 {"status":true,"queued":true,"sha":"..."}` |
| Deploy branch while another deploy is active | `202` with `queued:false`; the newest commit is retained and runs next |
| Other branch, ping, non-push event, or branch deletion | `200` with `ignored:true` |
| Coordination or queue unavailable | `503`; no blind deployment starts |

The deployed commit is always the webhook's head SHA, never a branch name
resolved later.

## `POST /api/edge/deploy`

Manually deploy a named commit:

```json
{"sha": "b3f2c81d9e..."}
```

This requires an authenticated user with the global `manage_deploy`
permission. API keys and member-scoped grants do not qualify. `sha` accepts
7–40 hexadecimal characters and is normalized to lowercase; an invalid value
returns `400`, while an unauthenticated or unauthorized request returns
`401`/`403`.

Responses use the same `202`/`503` shape as the webhook and do not expose
the internal deployment UUID.

## What success means

A node reports success only after nginx accepts the installed configuration
and the restarted candidate API returns exactly HTTP 200. Redirects do not
count. A candidate that cannot load Django is rolled back without requiring
the candidate API or management command to coordinate that rollback.

The trigger itself has no polling endpoint. Platform operators can inspect the
durable deployment journal through `GET /api/account/admin/platform` and use
the `edge_deploy` incident stream for node failures.
