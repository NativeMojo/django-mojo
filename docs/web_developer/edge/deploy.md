# Fleet code deploy — API

Two endpoints trigger a fleet code deploy. Everything else — canary, migration
locking, rollback — happens fleet-side; see
[django_developer/edge/deploy.md](../../django_developer/edge/deploy.md).

## `POST /api/github/deploy/webhook`

The GitHub push webhook. **Public, HMAC-gated**: the request must carry a
valid `X-Hub-Signature-256` (HMAC-SHA256 of the exact body with
`GITHUB_WEBHOOK_SECRET`); anything unsigned or mis-signed is `403`.

Point a GitHub webhook at it with content type `application/json` and the
shared secret. Behavior:

| Push | Response |
|---|---|
| To the deploy branch (`EDGE_DEPLOY_BRANCH`, default `main`) | `202` — `{"status": true, "queued": true, "sha": "<head sha>"}` |
| To the deploy branch while a deploy is in flight | `202` — `{"queued": false}`; the new commit is **recorded** and deployed when the in-flight deploy reaches its terminal (never a second concurrent deploy) |
| To any other branch, a `ping`, any non-push event, or a branch deletion | `200` — `{"ignored": true, "reason": ...}` |
| While Redis is unreachable | `503` — nothing recorded, nothing deployed |

The deployed commit is always the **payload's head SHA** — never a branch name
resolved later.

## `POST /api/edge/deploy`

Manual deploy of a named commit.

```json
{"sha": "b3f2c81d9e..."}
```

**Permission: global `manage_deploy`** (checked with `requires_global_perms`).
A member-level `manage_deploy` grant does **not** qualify, with or without a
`group` parameter — moving the fleet is a platform action, not a tenant one.
API keys are refused. `sha` accepts 7-40 hex characters (case-insensitive; a
branch name is a `400`).

Responses match the webhook: `202` with `queued` true/false, `503` when
coordination state is unavailable.

## Observing a deploy

There is no polling endpoint for deploy progress — the durable record is the
incident stream (`category "edge_deploy"`): level 7 for a canary failure,
timeout, or a node that failed to converge. Operational state on a node is
readable with `manage.py deploy_status get`.
