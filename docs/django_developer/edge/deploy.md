# Fleet code deploy — webhook, canary, locked migrations

How API code reaches every node in a multi-node deployment. A push to the
deploy branch converges the fleet onto **one commit and one framework
version**, migrations run **exactly once under a real Postgres advisory
lock**, and a **canary node proves the release** before anyone else takes it —
a broken release takes down one node, not the fleet.

REST surface for API consumers:
[web_developer/edge/deploy.md](../../web_developer/edge/deploy.md).

## The flow

```
GitHub push ──► POST /api/github/deploy/webhook        (HMAC-verified)
                  record DEPLOY_TARGET (last writer wins)
                  arm DEPLOY_STATUS=migrating (SET NX) ──► publish deploy_orchestrate

orchestrator (whichever job runner takes it):
  load the UUID attempt and its frozen edge-channel runner roster
  canary = lowest alive runner id that is not me
  tell the canary to update WITH --migrate, then poll DEPLOY_STATUS

  canary's script: install pinned commit + framework ► migrate_locked
                   ► sanity_check ► deploy_status set deploying|failed

  failed / silent ─► incident (level 7). Fleet stays on the old release.
  deploying       ─► tell every snapshot runner (same commit, same version),
                     then: chain check ► clear status ► update MYSELF last,
                     fire-and-forget
```

Single-runner fleets degrade cleanly: no canary is possible, the one node
updates itself with `--migrate`, and the status tail is cleaned by its TTL.

## Why there is no load-balancer walk

The obvious design — deregister → drain → install → verify → re-register per
node — was considered and **rejected**, and should not be re-proposed:

- `mojo/apps/realtime/` serves websockets. Draining helps request/response
  traffic; a websocket is severed by the restart no matter how carefully HTTP
  is drained. Zero-downtime is not achievable here, only approximated.
- ELB write privileges on the app's AWS identity — long-lived static keys
  present on every node — would turn any app-level compromise into a one-call
  total outage.
- The prize was ~30s of partial availability instead of 2-5s of clean
  downtime, with sockets dropped either way. Clients reconnect in ~5s.

The walk's real value was failure containment, and the canary buys that for a
fraction of the machinery.

## The deploy state (Redis)

Two keys, both TTL'd (`EDGE_DEPLOY_STATUS_TTL`, default 900s), accelerate the
durable `edge.PlatformDeployment` journal:

| Key | Holds | Semantics |
|---|---|---|
| `edge:deploy:target` | deployment UUID + commit SHA + who asked | **Last writer wins.** A push mid-deploy overwrites it; the orchestrator's chain check deploys it next. |
| `edge:deploy:status` | `migrating` / `deploying` / `failed`, stamped with UUID + SHA | Armed with `SET NX`. Terminal writes and deletion are **compare-and-set on UUID and SHA** (Lua), so an older attempt cannot settle a newer same-SHA retry. |

The TTL is load-bearing: a canary that dies hard would otherwise leave
`migrating` set forever and wedge every future deploy. The orchestrator clears
the status at its terminal; the TTL is the backstop, and the only cleaner on a
single-runner fleet. Redis is not durable. `PlatformDeployment` retains request
identity, its frozen roster, transitions, and the latest bounded proof per
runner; incidents remain the alerting trail.

Deploy jobs are published `max_retries=0` with an expiry: a node updating
itself kills its own job engine mid-job by design, and a redelivered deploy
job would re-run a whole update — possibly concurrently with the still-running
orphaned script.

## `migrate_locked` — why a flag file was not a lock

`var/allow_migrate` was a per-box flag: two boxes that both had it migrated
concurrently, and Django's `migrate` is not concurrency-safe.

```
manage.py migrate_locked [--noinput]
```

takes `pg_try_advisory_lock` (namespace 1423) and runs `migrate` **in the same
process** — an advisory lock is session-scoped, and the job engine calls
`close_old_connections()` per job execution, so a lock taken in an
orchestrating job would be gone before the migration ran. Non-blocking on
purpose: a second invocation **exits non-zero instead of queueing**. This also
covers what the canary cannot: a hand-run `post_deploy.sh` on another box
racing the deploy. The lock is released in a `finally`, including after a
failed migrate. Non-Postgres engines migrate without locking (unit-test
databases), with a warning.

## `sanity_check` — the canary is only as good as this

```
manage.py sanity_check [--url http://127.0.0.1/api/version]
                       [--timeout 5] [--retries 10] [--delay 2]
```

Five checks, stopping at the first failure, exit non-zero naming it:

1. Django apps ready.
2. Database reachable (trivial query).
3. No unapplied migrations.
4. Redis reachable.
5. **One real request served over the local socket** — the same
   `/api/version` probe `post_deploy.sh` uses.

Check 5 is the one that matters: 1-4 pass on code that cannot serve a request.
No AWS calls, nothing beyond localhost — it runs on a node mid-deploy.

## `deploy_status` — the update script's reporting contract

The skeleton's update script stops the job engine that is running the
`deploy_node` job which shelled it, so **Python after the script call never
executes on a self-updating node**. The script therefore reports terminal
status itself — and never reimplements the Redis conventions in bash:

```
manage.py deploy_status get
manage.py deploy_status set deploying --sha <target-sha> --deployment <uuid>
manage.py deploy_status set failed    --sha <target-sha> --deployment <uuid> --detail <known-phase>
```

`set` is compare-and-set on the stamped UUID and SHA. Exit 0 = applied; **exit 3** =
ignored because the deploy was superseded (distinct from argparse's 2, so the
script can tell "stale, fine" from "called wrong"). `--detail` is reduced to
a fixed allowlisted phase before it enters Redis, evidence, or an incident;
arbitrary callback text becomes `update_failed`. Process stdout/stderr and
provider exception messages never enter durable or operator-facing surfaces.

## Settings

All read with `settings.get_static` — **never** `settings.get`. A DB-backed
`Setting` row is REST-writable, which would make the update-script argv, the
deploy branch and the PyPI URL attacker-controlled data for any holder of a
global `manage_settings` grant.

| Setting | Default | Meaning |
|---|---|---|
| `EDGE_DEPLOY_SCRIPT` | *(unset — deploys refused)* | Update-script argv as a list, e.g. `["sudo", "-n", "/opt/api/aws/update.sh"]`. No default on purpose: an unconfigured box (dev laptop, CI) must refuse to sudo-run a guessed path. |
| `EDGE_DEPLOY_BRANCH` | `main` | Only pushes to `refs/heads/<this>` deploy. |
| `EDGE_DEPLOY_STATUS_TTL` | `900` | Seconds before an orphaned target/status expires. |
| `EDGE_DEPLOY_CANARY_TIMEOUT` | `600` | How long the orchestrator waits for the canary, and the expiry on deploy jobs. Keep it below the status TTL. |
| `EDGE_PYPI_URL` | `https://pypi.org/pypi/django-mojo/json` | Where the framework version is resolved, once per deploy. A resolution failure **fails the deploy** — a silently skipped upgrade is the failure mode the unpinned-upgrade policy exists to prevent. |
| `GITHUB_WEBHOOK_SECRET` | — | Reused from the github app; the webhook's HMAC key. |

The node's script is invoked as:

```
<EDGE_DEPLOY_SCRIPT...> --sha <40-hex> --framework <version> --deployment <uuid> [--migrate]
```

with both values pattern-validated before they enter the argv (no shell, no
interpolation — same seam discipline as the installer).

> The node-side half (`update.sh`, `post_deploy.sh`) ships inside the package
> under `mojo/deploy/scripts/`, executed through each project's `aws/` shims —
> `EDGE_DEPLOY_SCRIPT` keeps naming the project shim path (see `../deploy/README.md`).

## Required skeleton changes (separate item — this app alone does not deploy)

The django-mojo side is complete but inert until the skeleton's scripts speak
the contract above:

- `update.sh` takes `--sha` and checks out **that commit**, not
  `git reset --hard origin/main`; holds an `flock` so overlapping invocations
  on one box are impossible; short-circuits when already on the target SHA;
  calls `deploy_status set` at its terminals. On a `--migrate` failure it
  reports `failed` **first** and only then rolls back — the rollback may
  reinstall a framework version that predates `deploy_status`, so the report
  must happen while the reporting tool is guaranteed to exist.
- `post_deploy.sh` runs `manage.py migrate_locked --noinput` instead of the
  `var/allow_migrate` gate (which becomes deletable), and installs
  `pip install django-mojo==<the --framework value>` instead of `--upgrade`.

## Readiness proof versus deploy runners

Job runners are nodes in the fleet, not a separate operator concept. System
Setup filters heartbeat discovery to runners consuming the `edge` channel and
targets only those runner ids for proof. Each response reports the file-only
`EDGE_NODE_ID`, installed django-mojo version, and per-pool generation evidence.
The protected `EDGE_EXPECTED_TOPOLOGY` is the expected inventory; every declared
node/pool pair must answer and match before deployment readiness is green.
