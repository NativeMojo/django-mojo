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
                  arm DEPLOY_STATUS=migrating (CAS: absent or failed) ──► publish deploy_orchestrate

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
Multi-runner fleets are the normal case, and there the orchestrator is a
*different* node from the canary — which is what makes the node-failure
reporting below observable at all: the reporting node lives to report.

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
| `edge:deploy:status` | `migrating` / `deploying` / `failed`, stamped with UUID + SHA | Armed with a **Lua compare-and-set: nothing armed, or the armed lease is terminal `failed`.** Terminal writes and deletion are **compare-and-set on UUID and SHA** (Lua), so an older attempt cannot settle a newer same-SHA retry. |

The invariant is **never a second concurrent deploy while one is live** — not
"never a second deploy for the whole TTL". A `migrating` or `deploying` lease
is never stealable, so a same-second webhook race still starts exactly one
deploy. A terminal `failed` lease describes a deploy that is *over*, and the
next push re-arms straight over it.

The TTL is load-bearing: a canary that dies hard would otherwise leave
`migrating` set forever and wedge every future deploy. The orchestrator clears
the status at its terminal; the TTL is the backstop, and the only cleaner on a
single-runner fleet. Redis is not durable. `PlatformDeployment` retains request
identity, its frozen roster, transitions, and the latest bounded proof per
runner; incidents remain the alerting trail.

The TTL is no longer the *only* thing between a wedge and the next deploy. The
five-minute reconciler (`cronjobs.reconcile_platform_deployments`) does two
things before it closes anything:

- **`deploy.resume_stranded_target()`** republishes one `deploy_orchestrate`
  for a target whose deploy never started — something armed the lease and then
  died before publishing, or the orchestrate job expired undelivered. Guards:
  no live lease, the target names a valid SHA and UUID, that row still says
  exactly `requested` with a matching SHA, and `arm_status` lands (the atomic
  claim — two reconcilers race and one wins). At most one publish per sweep.
- **`platform_deploy.reconcile_stale()`** no longer skips every row that owns
  the live lease. A lease that is `migrating`/`deploying` is still hands-off,
  but a **terminal `failed`** lease closes its row `failed`
  (`reason: node_reported_failed`) and releases the lease — that is the case
  where the orchestrator which would have closed it is exactly what died. The
  `requested` row named by the live target is exempt from the `unknown`
  stale-closure, because resume owns it.

## When a node cannot run the update script

`deploy_node`'s job is to shell the update script, and the script reports its
own terminal status because it stops the engine that ran it. But there are
outcomes the script never gets to report, and **every one of them now reports
itself** through the same path — incident (level 7, `edge_deploy`), durable
per-runner evidence, and the deploy lease:

| `phase` | What happened |
|---|---|
| `unconfigured` | `EDGE_DEPLOY_SCRIPT` is not set on this node — the deploy is refused. |
| `preflight_failed` | The configured script has an explicit path and no execute bit. |
| `contract_mismatch` | The script is a fork that does not speak this framework's argv contract (below). |
| `exec_failed` | Exec raised (`OSError`) — wrong mode, missing interpreter, gone. |
| `script_timeout` | The script exceeded `SCRIPT_TIMEOUT` (900s) and was killed. |
| `update_script` | The script ran and exited non-zero. |

Two rules govern what a node's failure is allowed to touch:

- **The lease is written only when this node was migrating** (the canary, or a
  single-runner fleet). When the CAS lands, the durable row is closed `failed`
  too — the same close the script-reported path performs. A **fleet** node's
  failure never touches the deploy status: the canary already proved the
  release, and one node falling behind is an incident about that node, not a
  failed deploy.
- **Evidence carries a bounded stderr tail; incidents never do.** The last ten
  lines, sanitized **one line at a time**, so a credential-shaped line
  collapses to `[redacted]` without taking the surrounding diagnosis with it.
  (POSIX `TimeoutExpired` carries bytes even under `text=True`; the tail
  decodes explicitly, and `platform_deploy._safe` routes bytes through the
  redactor rather than stringifying them past it.)

### `aws/update.sh` must ship committed 100755

The deploy plane `exec()`s the configured path directly. A shim committed
`0644` refuses the deploy on **every node in the fleet at the same moment** —
the failure looks fleet-wide because it is. `deploy_node` probes the execute
bit before it starts (explicit paths only: a bare `sudo` is skipped, because
`os.access` does no PATH resolution and probing it would refuse every deploy on
the documented argv), and `check_node`'s **shims** section audits the mode:

```
git update-index --chmod=+x aws/update.sh && commit the mode
```

A local `chmod` does not survive a clean checkout, which is why the mode has to
be in the index. `aws/post_deploy.sh` is exempt — `update.sh` invokes it as
`sudo bash <path>`.

### The node-script contract

Contract **v1** is the argv the deploy plane speaks to the node script:

```
--sha <7-40 hex> --framework <version> --deployment <uuid> [--migrate]
```

The number lives in two places that must agree: `deploy.DEPLOY_CONTRACT`, and a
marker line at the top of the packaged `mojo/deploy/scripts/update.sh`:

```bash
# mojo-deploy-contract: 1
```

`deploy_node` **reads** the configured script before it execs it (never runs
it to ask — that would mean executing an unknown path to decide whether it is
safe to execute) and walks a ladder, most authoritative first:

| Verdict | How it was reached | Deploy |
|---|---|---|
| `declared` | The marker is present; the integer after it is the answer. | Proceeds, unless it declares a contract **older** than this framework's. |
| `shim` | The file references `mojo.deploy` — the same adoption predicate `check_node`'s shims audit uses. | Always proceeds. |
| `inferred` | No marker, but the file parses argv literally and every required flag appears. | Proceeds. |
| `stale` | Parses argv literally and a required flag is **missing**. | **Refused**, naming the first missing flag. |
| `unknown` | Unresolvable path, unreadable file, an unparseable marker, a `"$@"` forwarder, or a file that never mentions `--sha`. | Proceeds. |

Two asymmetries are the whole design:

- **Only `declared`-behind and `stale` can refuse.** Everything this guard
  could not read confidently proceeds. It exists to name a stale fork before it
  wastes a deploy, not to become a new way for a deploy to fail on a node whose
  script it simply could not read.
- **A `"$@"` forwarder is never condemned.** A wrapper that forwards argv
  wholesale does not inspect the flags, so `--sha` appearing in its own usage
  comment is not evidence about what the real entry point accepts.

**Shims cannot drift**, which is the point: a shim delegates to the packaged
body, so its contract is whatever the installed framework ships. The guard is
for **forks** — a project that copied `update.sh` instead of shimming it, whose
argv froze at the generation it was copied. A fork taken before `--deployment`
existed fails in the worst way available: the plane execs it, it refuses (or
silently drops) the flag, and the deploy dies minutes later as *"the canary did
not report"* with nothing naming the cause.

A refusal reports on every surface the other node-side refusals do — durable
evidence (`phase: contract_mismatch`, with the contract read, the contract
required, and the missing flag), the deploy lease from a migrating node, and a
level-7 incident titled *Edge deploy node script contract mismatch*:

```
deploy <uuid> (<sha>): node update script speaks deploy contract v0; this
framework requires v1 — the script named by EDGE_DEPLOY_SCRIPT is a stale fork
(see django-mojo docs/django_developer/edge/deploy.md)
```

The packaged script also answers the question directly, for an operator or a
checker on the box:

```bash
aws/update.sh --contract     # prints 1, exits 0, touches nothing
```

It is answered before the `cd`, so it works on a box whose `PROJ_PATH` does not
exist yet, and combining it with any other flag is a usage error. The deploy
path never uses it — it reads the marker.

### Release rule — one framework generation back, in both directions

Node scripts and the framework are installed at different moments (a shim runs
the body from the framework installed *before* the run — see
`../deploy/README.md`), so **both** halves must tolerate one generation of skew:

- **A framework at contract N must keep reading the artifacts written under
  N−1** — the argv it is handed back, the environment, and on-disk state such
  as `/etc/mojosec/config.json`. A node that has not deployed yet is still
  running the previous generation's files.
- **A script declaring contract N must keep accepting the argv a framework at
  N−1 emits.** The orchestrator on the old release is the one that will invoke
  the newly installed script.

Breaking either direction requires a compatibility window (accept both shapes
for one release) **or** a `DEPLOY_CONTRACT` bump plus a migration note telling
operators to update forks first. A bump is not a free rename: it refuses every
fork on the fleet at the same moment, which is the intended blast radius only
when the old argv genuinely cannot be honored.

### A superseded orchestrator stands down

While polling for its canary, the orchestrator re-reads the lease. If the lease
is gone or belongs to another deployment, this deploy has been superseded: it
transitions `superseded` and returns immediately — **no** canary-failure
incident (the canary was never going to report to it) and **no** chained
orchestrate on top of the deploy that took the lease. The chain re-arm at the
terminal carries the same guard: if the new target is already armed, it is left
to its own orchestrator.

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

The 1.9-to-UUID rollout has one compatibility seam: an already-running 1.9
canary script cannot add `--deployment` after it installs the new framework.
The command accepts that callback only when Redis still holds the same SHA in
an empty-UUID legacy lease, and writes no journal evidence for it. A lease that
contains a UUID always requires the matching `--deployment`; the bridge cannot
claim or settle a new attempt.

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
| `EDGE_FRAMEWORK_VERSION` | *(unset — newest published release)* | **A protected system setting, not a file setting** — the one exception below. The operator's hold on which django-mojo version every deploy installs. |

### `EDGE_FRAMEWORK_VERSION` — the operator's framework hold

Every other setting here is deployment-owned and read from the file. This one
is deliberately **portal-writable**, because its whole purpose is to let an
operator freeze the framework without shipping a deploy. It is safe for the
same reason `EDGE_EXPECTED_TOPOLOGY` is: it is a **protected** system setting,
so the generic `Setting` write path refuses it and its only writer
(`POST /api/account/admin/advanced/settings`, superuser + fresh interactive
auth, no key-backed sessions) re-reads an active literal superuser first. A
global `manage_settings` grant cannot reach it.

Three accepted forms:

| Value | Deploy installs |
|---|---|
| unset (or `latest` / `none` / `auto` / empty) | The newest published release from PyPI. The default, and the behavior that predates this setting. |
| a published version, e.g. `1.11.9` | Exactly that version, with **no PyPI request at all** — a pinned fleet still deploys while PyPI is unreachable. |
| `hold` | The framework version of the last **converged** deploy: ship the commit, keep the framework. |

Anything else is refused at the portal, with a message naming the accepted
forms — `stable` and `v1.2.3` are the two shapes operators reach for, and
neither is a version pip can install. Case is normalized (`HOLD` → `hold`,
`1.0.0RC1` → `1.0.0rc1`, matching PEP 440). The value is re-normalized at read
time too, so a row written before the validator existed becomes a clean
refusal rather than a deploy argv.

Semantics that matter operationally:

- **Read live, at orchestrate time.** A change applies from the *next* deploy;
  it never moves one already running. The version is resolved once and carried
  in every node payload, so a hold changed mid-deploy cannot split the fleet
  across two framework versions — the node installs what it was told.
- **An unusable hold refuses the deploy**, with a level-7 incident titled
  *Edge deploy framework pin is unusable*. That covers a junk row and `hold` on
  a fleet that has never converged. There is deliberately no fallback: latest
  is precisely what the operator asked not to have. The incident names the
  setting and which way it is broken, never the stored value.
- **Nothing validates the version against PyPI at write time.** A pin to a
  version that does not exist is accepted and dies at `pip install` on the
  canary, with pip's own message — which is the right place for "no such
  release" to be discovered.
- `hold` holds at **converged**, not merely released: converged is the
  reconciler's proof that every frozen runner answered with the deployment's
  UUID and SHA. Holding at a dispatch would freeze the fleet onto a version
  that may never have booted.

The Platform overview reports it as `deployments.data.framework_pin`
(`configured` / `value` / `mode` / `resolved`); see
[web_developer/account/admin_portal/platform.md](../../web_developer/account/admin_portal/platform.md).

The node's script is invoked as:

```
<EDGE_DEPLOY_SCRIPT...> --sha <40-hex> --framework <version> --deployment <uuid> [--migrate]
```

with both values pattern-validated before they enter the argv (no shell, no
interpolation — same seam discipline as the installer).

> The node-side half (`update.sh`, `post_deploy.sh`) ships inside the package
> under `mojo/deploy/scripts/`, executed through each project's `aws/` shims —
> `EDGE_DEPLOY_SCRIPT` keeps naming the project shim path (see `../deploy/README.md`).

## What the node script does (and what a fork has to preserve)

Both node scripts **ship inside django-mojo** (`mojo/deploy/scripts/`) and run
through a three-line project shim, so a project on the shim needs no changes at
all — the behavior below moves with every `pip install`. It is written down
because a **fork** owns all of it, and because the deploy plane depends on it:

- `update.sh` takes `--sha` and checks out **that commit**, not
  `git reset --hard origin/main`; holds an `flock` so overlapping invocations
  on one box are impossible; short-circuits when already on the target SHA and
  framework; calls `deploy_status set` at its terminals. On a `--migrate`
  failure it reports `failed` **first** and only then rolls back — the rollback
  may reinstall a framework version that predates `deploy_status`, so the
  report must happen while the reporting tool is guaranteed to exist.
- `post_deploy.sh` runs `manage.py migrate_locked --noinput` (never a
  `var/allow_migrate` flag file, which is not a lock) and installs
  `pip install django-mojo==<the --framework value>` — pinned, never
  `--upgrade`, because the version was resolved once for the whole fleet.
- A fork must additionally declare the contract marker, or at minimum accept
  every required flag; otherwise the deploy plane refuses it (above). The
  supported answer to "our node script needs a local delta" is a shim with the
  delta in its exported variables, not a copy.

## Readiness proof versus deploy runners

Job runners are nodes in the fleet, not a separate operator concept. System
Setup filters heartbeat discovery to runners consuming the `edge` channel and
targets only those runner ids for proof. Each response reports the normalized
system hostname (or optional file-only `EDGE_NODE_ID` override), installed
django-mojo version, and per-pool generation evidence.
The protected `EDGE_EXPECTED_TOPOLOGY` is the expected inventory; every declared
node/pool pair must answer and match before deployment readiness is green.

Deployment roster discovery reads the channel's dedicated timestamped runner
index from the Redis primary, then pipelines the corresponding TTL heartbeat
documents. It never scans the shared Redis keyspace, so unrelated cache, queue,
or non-edge runner volume cannot consume the roster's bounded discovery budget.
The index query reads at most the configured roster limit plus one entry;
overflow, an empty roster, a missing, malformed, mismatched, stale, or
implausibly future-dated declaration, or a timeout fails platform and WebApp
deployments closed.

A canary success remains `fleet` while restarted runners repopulate their
heartbeats. The five-minute reconciler waits through the restart grace period,
then collects UUID/SHA proof from every frozen runner and closes the attempt as
`converged`, `partial`, or `unknown`; canary proof alone is never reported as
healthy fleet convergence.

Job engines synchronously register before initialization succeeds, refresh each
consumed-channel index before their heartbeat document, prune expired entries,
refresh a bounded expiry on each index key, and remove their own entries on
graceful shutdown. A crashed engine ages out of discovery after three heartbeat
intervals, and an abandoned one-runner channel index expires. When upgrading from a release that
predates these indexes, restart every job engine and wait for one heartbeat
before triggering the first deploy; no manual Redis seeding is required.
