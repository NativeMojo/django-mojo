# Fleet code deploy

A verified GitHub webhook records the requested commit and starts the existing
fleet fan-out. Each attempt freezes two live runner cohorts:

- `edge` is the API cohort;
- `platform-deploy` is the specialized/code cohort.

Their union, capped at 128 unique runners, is the deployment roster. A runner
advertising both channels is classified as API. A deployment with no live API
runner fails before framework resolution, migration, or node mutation because
there is no safe migration canary.

The GitHub request key is derived from the commit SHA, not the delivery id.
Separate deliveries for one unchanged commit therefore reuse the same durable
attempt and cannot restart the fleet twice. An intentional same-SHA recovery
still creates a new attempt through the Admin **Retry same SHA** action, whose
source is `admin_retry` rather than `github`.

## Typed routing and canary

Every node declares its lifecycle with file-only `EDGE_DEPLOY_NODE_TYPE`:

| Value | Job channel | Lifecycle |
|---|---|---|
| `api` (default) | `edge` | Built-in Django/nginx activation |
| `code` | `platform-deploy` | Checkout and dependencies only |
| custom name, such as `sites` | `platform-deploy` | `aws/deploy/<type>.sh` profile |

The canary is selected only from the frozen API cohort, and only that canary
receives `--migrate`. Each node job also carries its expected cohort; a mismatch
between that route and the node's static type fails before the shell command.
After the API canary proves the candidate, the same commit and framework
version fan out to both cohorts.

A specialized node must consume `platform-deploy` in `JOBS_CHANNELS`; an API
node must consume `edge`. Actual jobs remain addressed to each runner's direct
channel. See [Project deployment scripts](../deploy/README.md) for the custom
profile and rollback contracts.

## The deliberately small node contract

The parent runs `EDGE_DEPLOY_SCRIPT`, which defaults to the permanent packaged
locator, with:

```text
--sha <commit> --framework <version> --deployment <uuid>
--node-type <type> [--migrate]
```

Projects may override the complete argv. A custom override that crosses
`sudo` before reaching the packaged updater must place the private
`--parent-status` bit after that boundary, for example
`["sudo", "-n", "/opt/api/aws/update.sh", "--parent-status"]`. Alternatively,
launch a locator shim as the application account and let the packaged updater
carry the marker through its own elevation. An existing `aws/update.sh` that
delegates to `python3 -m mojo.deploy locate update.sh` therefore needs no
vendored framework body, but its launch shape must preserve this status
contract. SHA, framework version, deployment UUID and node type are validated
before mutation. The parent does not inspect script source, rewrite a custom
argv, or add deployment security-policy gates.

For an API node, zero exit means:

- the candidate loaded through `manage.py check`;
- the real `nginx -t` accepted the installed configuration; and
- the restarted candidate API returned exactly HTTP 200.

Redirects do not count as API health. A candidate that cannot import Django
fails before host configuration is changed and is rolled back entirely by
shell code. A `code` node has no activation gate beyond the common install; a
custom node succeeds only after its own `preflight`, `restart` and `probe`
profile verbs return zero.

There is deliberately no TLS semantic parser, certificate-lineage
preservation gate, node-role authority, RPM verification, file-integrity gate,
trusted-change journal, ownership policy, or request-service policy in release
acceptance. Those systems may observe and alert independently, but cannot veto
a deploy.

## Transaction and status ownership

The update immediately re-enters a transient systemd oneshot before checkout
or package mutation. The unit owns a 30-minute activation limit, followed by
up to 15 further minutes for TERM-triggered rollback if systemd stops a
timed-out transaction. Restarting the job engine therefore cannot orphan an
update. The parent process waits beyond both windows instead of killing a
legitimate rollback.

Before an API activation commits, the root transaction requires the host cron
service to be active, repairs only Jobman's pid and log files to the exact
account in the installed jobs cron, and runs, as that account, a no-spawn launch
preflight through the same fixed system Python and installed Jobman module used
by cron. This catches the practical outage cases without making deployment
depend on the security sensor: a stopped cron daemon, a missing module, stale
root ownership, or an unwritable runner surface leaves the current engine
running and rolls the candidate back.

Current parents record node evidence after the script returns. API nodes then
detach a bounded, journaled root stop of both the job engine and scheduler so
the completed job can be acknowledged before the old processes exit. The stop
logs under `mojo-deploy-recycle` and exits nonzero rather than logging success
if any process survives. Because the handoff is detached after node evidence is
recorded, that failure is an operational alarm rather than a rollback of the
activated release. The installed every-minute cron entry starts both
replacements in a fresh audit session; starting them from the retiring engine
would inherit its session and prevent MojoSec from proving JobEngine-originated
firewall work. `code` nodes receive no generic restart. A custom profile owns
its service restart; if that restart kills the caller, the replacement engine
consumes the transaction's bounded outcome and exact local identity to finalize
the same deployment UUID.

One predecessor-generation callback remains solely for API adoption: when the
parent does not set `MOJO_DEPLOY_PARENT_STATUS`, the healthy migrating canary
reports legacy status once. It never runs before nginx and HTTP checks, and it
never runs on non-API nodes.

## Failure handling

An invalid type/cohort, missing custom profile, exec error, timeout, or non-zero
script exit is reported as node failure. Mechanical state under
`/var/lib/django-mojo-deploy/active` restores the previous checkout, declared
dependencies, exact framework and typed lifecycle. An interrupted transaction
is recovered before the next candidate starts. The final node diagnostic uses
one fixed phase and rollback result, such as
`Deployment failed during django_check; rollback completed`; it does not
stream or parse candidate tracebacks. A failure after activation has committed
keeps the candidate live and reports the distinct fixed result `publication
completed` or `publication recovery failed` instead of claiming a rollback.

Redis remains short-lived coordination and `PlatformDeployment` remains the
durable attempt record. Both the installed identity and node evidence include
the node type. Neither adds another release gate; post-activation MojoSec
convergence and independent observation remain outside release acceptance.

## Queue capacity and the coordination lease

The deploy plane never competes with ordinary work for a worker. The
orchestrator is published on the `priority` channel and every node update on
that node's box-direct channel; both are **reserved channels** in the job
engine, which keeps `JOBS_ENGINE_RESERVED_WORKERS` slots that only reserved
channels may claim (see [jobs settings](../jobs/settings.md#engine-configuration)).
A node whose other workers are all busy — ten file renditions per box was the
2026-09-18 shape — still starts its deploy job. `priority` must therefore be in
`JOBS_CHANNELS` on every node that can orchestrate (it is a framework default
channel; a project that sets `JOBS_CHANNELS` explicitly must list it).

The Redis status lease (`EDGE_DEPLOY_STATUS_TTL`) is a crash backstop, not a
deadline on a deploy that is being driven. The orchestrator renews its own
lease when it starts and on every canary poll (`deploy.touch_status`, an
owner-gated Lua `EXPIRE`), so time the orchestrate job spent queued never
counts against the canary window. The lease then only expires for an
orchestrator that stopped touching it — exactly the crash it exists for.

The orchestrator now tells two things apart that used to share one branch:

| Lease state mid-canary | Recorded as | Incident |
|---|---|---|
| names **another** deployment | `superseded` / `lease_superseded` — a newer deploy took the plane; stand down quietly | none |
| **absent** (expired or flushed, nobody armed) | `failed` / `coordination_lease_expired` — the fleet is still on the previous release and nothing will retry by itself | `Edge deploy lost its coordination lease`, naming the canary being waited on |

The same distinction applies before the canary is dispatched: coordination
keys that expired while the orchestrate job sat in the queue fail the attempt
with the same reason and an incident saying the lease died *before the
orchestrator ran*, instead of the old `target_moved_before_start`
supersession. A genuinely moved target is still chained.

A canary that never reports is diagnosed from its own `Job` row and the
verdict travels in both the incident and the row's failure detail
(`diagnosis.state`): `never_started` — the job expired pending on the
canary's box-direct channel because that engine had no free worker — or
`started`, meaning the node took the job and the update script is what went
quiet. Recovery for every failed attempt is the Admin **Retry same SHA**
action once the cause is cleared.
