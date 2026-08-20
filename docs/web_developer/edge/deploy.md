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
| To the deploy branch while a deploy is in flight | `202` — `{"status":true,"queued":false,"sha":"<head sha>"}`; the new commit is **recorded** and deployed when the in-flight deploy reaches its terminal (never a second deploy *while one is live*) |
| To the deploy branch after the previous deploy **failed** | `202` — `queued` is **`true`**: a failed deploy is over, so the next push starts immediately rather than waiting out the previous attempt's coordination TTL |
| To any other branch, a `ping`, any non-push event, or a branch deletion | `200` — `{"status":true,"ignored":true,"reason":"..."}` |
| While the runner roster, Redis coordination, or queue publication is unavailable | `503` — no blind deploy starts; the durable attempt is retained as `failed` with a classified reason |

The frozen edge roster must be non-empty and complete within its bounded
discovery budget. Runners maintain a dedicated time-windowed index for each
consumed channel, so unrelated Redis key volume and non-edge workers do not
affect discovery. Platform triggers return `503` for roster overflow or a
missing, malformed, mismatched, stale, or implausibly future-dated declaration;
WebApp promotion restores its prior release. In both paths the attempt fails
closed, and the orchestrator never treats a truncated node list as the fleet.

After upgrading from a framework release that predates the channel indexes,
restart every job engine and wait for one heartbeat before the first deploy.
The engines populate and prune the indexes themselves; do not seed Redis by
hand.

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

Responses match the webhook: `202` with `status`, `queued`, and normalized
`sha`; `503` when coordination state is unavailable. The trigger response does
not reveal the internal deployment UUID.

## Observing a deploy

This external trigger has no polling endpoint. Operators with dedicated global
Platform access observe the durable UUID journal through
`GET /api/account/admin/platform`, whose `deployments` section carries bounded
transition and per-runner evidence. The incident stream (`category
"edge_deploy"`) remains the alerting trail: level 7 for a canary failure,
timeout, or a node that failed to converge. Operational state on a node is
readable with `manage.py deploy_status get`.

### `detail.phases` — where the deploy's time went

Each deployment in that section carries a `detail` object, and on any node that
reported one it holds `phases`: an ordered, bounded list of what the update
spent its seconds on.

```json
"detail": {
  "phases": [
    {"phase": "git_sync",       "pass": "deploy", "ms": 1204},
    {"phase": "deps",           "pass": "deploy", "ms": 8130},
    {"phase": "framework",      "pass": "deploy", "ms": 18042},
    {"phase": "restart",        "pass": "deploy", "ms": 2233},
    {"phase": "total",          "pass": "deploy", "ms": 41230},
    {"phase": "engine_restart", "pass": "deploy", "ms": 9600}
  ]
}
```

- `phase` — a short `[a-z_]` name. The set is documented in the framework
  guide; treat it as open and render an unknown name as-is.
- `pass` — `"deploy"` or `"rollback"`. A failed canary that rolled back reports
  both, in the order they happened, so the same phase name can legitimately
  appear twice.
- `ms` — milliseconds. `"approx": true` appears when the node could only
  measure whole seconds; the value is still in ms.

Everything about this list is optional and best-effort. An older node script
sends none, a node whose `date` cannot do milliseconds sends approximations,
and a malformed line is dropped rather than reported. **Never treat a missing
or short `phases` list as a failed deploy** — the deployment's `status`,
`transitions` and `node_evidence` remain the record of what happened.

Two entries are worth knowing about: `total` is the whole node run up to its
terminal callback, and `engine_restart` is measured by the platform rather than
the node — it covers the window in which the node had no job engine to measure
anything with — so it appears only once the deployment converges.
