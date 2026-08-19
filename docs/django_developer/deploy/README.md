# Node deployment tooling (`mojo.deploy`)

The node half of the django-mojo deploy plane, shipped inside the wheel and
run on the node itself, outside Django:

| Tool | What it does |
|---|---|
| `mojo.deploy.config_sync` | Pulls this node's `django.conf` from S3 and installs it atomically at 0600 |
| `mojo.deploy.check_setup` | Read-only audit of an AWS account for security and durability gaps |
| `mojo.deploy.certbot_sync` | Shares one Let's Encrypt lineage across a fleet via S3 — primary renews and pushes, replicas pull |
| `mojo.deploy.check_node` | Read-only audit of ONE node against the on-node deploy contract |
| `mojo.deploy.jobman` | Starts, stops and reports the node's **foreground** job engine and scheduler |
| `mojo.deploy.node_setup` | Converges `var/` ownership, systemd units, and the jobs cron |
| `mojo.deploy.mojosec` | Safely installs and operates the privileged, observe-only MojoSec sensor |
| `mojo.deploy.audit` | Converges selective Linux Audit execution provenance and publishes constrained health |
| `mojo.deploy.firewall_broker` | Root semantic firewall executor; accepts no argv and constructs closed operations |
| `mojo.deploy.provision` | Takes an **empty AWS account** to a running environment — eight prompts, a committed `aws/environments/<env>.json`, a priced preview, then an idempotent converge. Creates and modifies; never deletes. See [provision.md](provision.md) |
| `python3 -m mojo.deploy locate <name>` | Prints the absolute packaged path of `update.sh` / `post_deploy.sh` for the project shims |
| `python3 -m mojo.deploy render --dest …` | Materializes the packaged cron/systemd templates into `${PROJ_PATH}/var/deploy`. **Not the same thing as `mojo.deploy.provision.render`**, which builds and publishes an environment's `django.conf` to S3 — same verb, opposite direction: this one writes files on a node, that one writes an object a node reads |
| `mojo/deploy/scripts/update.sh` | The fleet update entry (deploy / manual modes) — packaged bash, run through a project shim |
| `mojo/deploy/scripts/post_deploy.sh` | Post-checkout convergence: deps → framework → migrate → render → nginx/systemd/cron → restart + probe |
| `mojo/deploy/provision/scripts/stage1.sh` | What a freshly launched EC2 node runs: untar the tree → `ec2_bootstrap.sh` → pin `django-mojo==<version>` → `ec2_deploy.sh` → `var/profile` → CloudWatch agent → `config_sync`. Published to the config bucket by `provision apply`, downloaded and exec'd by stage-0 user data. **Not** resolvable through `locate` — see below |
| `mojo/deploy/provision/scripts/cloudwatch-agent.json` | The agent configuration template stage 1 installs, with this environment's three log-group names substituted in by the CLI |

Everything is invoked with `python3 -m` (the bash scripts through their shims):

```bash
python3 -m mojo.deploy.config_sync --dry-run
python3 -m mojo.deploy.check_setup --section s3,iam
python3 -m mojo.deploy.certbot_sync --renew
python3 -m mojo.deploy.check_node --require-shims
python3 -m mojo.deploy.jobman status
python3 -m mojo.deploy.node_setup --dry-run
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec converge --mode observe)
python3 -m mojo.deploy locate update.sh
python3 -m mojo.deploy.provision apply --env prod --dry-run
```

MojoSec convergence snapshots every Audit rules source, generated and active
state before replacing the AL2023 `task,never` seed. Unknown operator rules or
concurrent changes stop deployment. Failed load/verification restores the
snapshot. The installed health timer owns `CAP_AUDIT_CONTROL`; the main sensor
does not. A root-owned stable helper lets the currently running deployment shim
restore the pre-feature rules when a release downgrades to a framework without
the feature. Broker-only units, sudoers and executable are then removed while
the intentionally retained legacy direct grants keep rollback operational.

The Audit publisher projects command output onto one closed v1 sidecar contract:
`schema`, `version`, `boot_id`, `generation`, `rules_sha256`, `sequence`,
`enabled`, `failure`, `rate_limit`, `backlog_limit`, `backlog`, `lost`, and
`updated_at`. OS telemetry such as `pid`, backlog wait times,
`loginuid_immutable`, and future `auditctl -s` fields is ignored at the command
boundary. Unknown or duplicate JSON sidecar keys, missing fields, booleans,
negative/oversized counters, and non-finite timestamps remain malformed and
fail closed.

The existing stop-last invariant in `update.sh` is also the explicit engine
cutover: Audit policy and the sensor are converged first, the old foreground
engine is stopped only after every fallible deployment step, and the installed
cron starts the new engine generation. Until the sensor has captured that
cron → jobman → engine exec chain and pinned its live PID generation, firewall
suppression remains disabled. `check_node` treats the missing post-cutover
anchor as a deployment failure; no old in-memory engine can attest new broker
work.

## Why these live here and not in each project

They used to be copied into every project from `django-mojo-skeleton`. The
skeleton is a template you clone, so a fix made in one copy never reaches the
others: `certbot_sync.py` forked into three repos in three different states
over a year, and one copy was a node away from silently breaking TLS on every
replica.

django-mojo is installed on every node and upgraded unpinned on every deploy.
Anything that must never drift belongs on that channel.

## Why `python -m` and not a management command

Circular bootstrap. `manage.py` loads Django settings; settings come from
`/opt/api/var/django.conf`; `config_sync` is the thing that puts that file
there. A management command could not run on a node that does not yet have a
config, which is the only interesting case.

`check_setup` is a `-m` program for a weaker reason: it audits an AWS account,
not a running deployment, and is routinely run from a laptop against an account
whose Django project is not installed locally.

`provision` is the strongest case of all: it runs against an account that has no
django-mojo in it *anywhere* yet — no node, no config bucket, no `django.conf` to
read — so there is nothing a management command could bootstrap itself from.

There are deliberately **no `[project.scripts]` console entry points**. Those
reintroduce a dependency on wherever pip put the script directory — the exact
coupling moving into the package removes. Skeleton nodes run no virtualenv
(see `django-mojo-skeleton/aws/ec2_deploy.sh`), so `/usr/bin/python3 -m ...`
resolves against the system interpreter's site-packages.

## THE RULE FOR THIS PACKAGE: `mojo.helpers.*` is off-limits

`from mojo.helpers import logit` raises

```
AttributeError: module 'mojo.helpers.paths' has no attribute 'LOG_ROOT'
```

when Django settings are not configured, because `logit.py` reads
`paths.LOG_ROOT` at module level and `paths.py` only creates that attribute
inside `configure_paths()`. The same import chain kills all of
`mojo.helpers.aws` (`__init__.py` → `s3.py` → `logit`), so `get_client()` and
`get_session()` are unusable here. **Build the boto3 client directly.**
Deferring the import into a function does not help — it fails at call time
instead.

Logging uses the repo's graceful-fallback idiom (`try: from mojo.helpers import
logit` / `except Exception: import logging`). On a bootstrap node the `except`
branch always fires; that is expected, not a bug.

### `mojo/__init__.py` is on the boot path

`python3 -m mojo.deploy.config_sync` imports the parent package first, so
`mojo/__init__.py` → `mojo.helpers.response` runs before a single line of
`bootstrap.conf` is parsed. It works today and is pinned by
`tests/test_deploy/import_isolation.py`, which spawns real subprocesses with
`DJANGO_SETTINGS_MODULE` stripped. If a future release adds a settings-touching
import anywhere on that path, config fetch breaks on every node of every
project at once — those tests are the only thing that would notice.

---

# `config_sync`

```
s3://<bucket>/<prefix>/django.conf            operator-owned base
s3://<bucket>/<prefix>/django.override.json   optional typed Admin overrides
                       \ /
                        v  verify, validate, compose
              /opt/api/var/django.conf   0600, atomic replacement
```

A pull in one direction only. S3 is authoritative; no node ever writes back.

## The install contract

These properties are the reason the module exists, and each is asserted by a
test in `tests/test_deploy/config_sync.py`:

- **0600, atomically, never briefly wrong.** `install()` does chmod → chown →
  `os.replace`, *in that order*. The file never appears at the destination with
  the wrong permissions, not even for an instant.
- **Staged on the same filesystem.** `tempfile.mkdtemp(dir=dirname(target))`
  gives a 0700 directory on the target's filesystem, which is what makes
  `os.replace` atomic rather than a copy.
- **Never installs nothing.** An empty published object, a sha256 mismatch, or
  a failed download all leave the existing config in place. A node with stale
  config serves; a node with no config does not start.
- **One writer.** `flock` on `.config_sync.lock` next to the target, so the boot
  oneshot and the timer cannot both write. The lock lives next to the target
  rather than in `/var/run` so that "can hold the lock" means "can write the
  config", identically for root and for the app user.
- **Stale staging is swept.** `rmtree` in a `finally` only covers the paths
  where Python unwinds; a SIGKILL or OOM mid-transfer runs no `finally` at all.
  Leftover `config_sync.*` directories are removed at the start of `sync()`,
  under the lock.

## `bootstrap.conf` keys

Read from `/opt/api/var/bootstrap.conf`, deliberately **not** from
`django.conf`: on a fresh node `django.conf` is the thing being fetched.

| Key | Required | Meaning |
|---|---|---|
| `AWS_REGION` | yes | Region for the S3 client |
| `AWS_CONFIG_BUCKET` | yes | Bucket holding the published config. **Absent = dormant**: one debug line, exit 0. |
| `AWS_CONFIG_PREFIX` | yes | e.g. `config/wmx/prod`. Set without a bucket-name it is an error — the tool refuses to guess which environment's config to pull. |
| `AWS_CONFIG_FILENAME` | no | Object name under the prefix (default `django.conf`). Its own setting, not `basename(--target)`, so moving where the file installs does not silently change which object is fetched. |
| `AWS_KEY` / `AWS_SECRET` | no | Omit to use the instance role. Setting exactly one warns and falls through to the ambient chain. |
| `CONFIG_SYNC_RESTART` | no | Restart the app when the config actually changes |
| `CONFIG_SYNC_OWNER` | no | `user:group` for the installed file. Unresolvable is a warning, not a refusal. |
| `CONFIG_SYNC_SERVICE` | no | Unit to restart (default `mojo-asgi.service`) |
| `CONFIG_SYNC_REQUIRE_SHA` | no | Refuse to install a published object with no `sha256` metadata |
| `CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS` | no | Comma-separated deployment delegation for typed Admin overrides. Empty or absent disables the override object. |
| `CONFIG_SYNC_OVERRIDE_FILENAME` | no | Override object name under the same prefix (default `django.override.json`). |

**`BOOTSTRAP_PATH` is hardcoded** at `/opt/api/var/bootstrap.conf`, unlike the
service name. It cannot itself come from bootstrap config, and `--config` is
the existing escape hatch.

### Publishing, and `CONFIG_SYNC_REQUIRE_SHA`

The integrity check compares the downloaded bytes against the object's
`sha256` **user metadata**. A plain `aws s3 cp` sets no such metadata, in which
case the check has nothing to compare against and silently passes. That case
now logs a warning ("published object carries no sha256 metadata — installing
unverified").

Publish with the metadata to get a real check:

```bash
SHA=$(shasum -a 256 django.conf | cut -d' ' -f1)
aws s3 cp django.conf s3://$BUCKET/$PREFIX/django.conf --metadata sha256=$SHA
```

Once every publisher does that, set `CONFIG_SYNC_REQUIRE_SHA=true` to turn a
missing digest into a refusal. It is **off by default** on purpose:
strict-by-default would break existing publishers the moment a node picked up
an unpinned django-mojo upgrade.

### Admin fleet overrides

The optional override object is deliberately not a second arbitrary
`django.conf`. It is a bounded JSON document whose keys must have framework
validators and must also appear in the node's
`CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS`. Config sync verifies its required
`sha256` metadata, rejects an unknown key or invalid type, and composes it after
the operator-owned base file. Any failure preserves the last working file.

The Admin publisher uses `ADMIN_FLEET_CONFIG_BUCKET`,
`ADMIN_FLEET_CONFIG_PREFIX`, `ADMIN_FLEET_CONFIG_FILENAME`, and
`ADMIN_FLEET_CONFIG_KMS_KEY_ID`; bucket and prefix fall back to the matching
`AWS_CONFIG_*` application settings, filename defaults to
`django.override.json`, and the KMS key falls back to `KMS_KEY_ID`.
`ADMIN_FLEET_CONFIG_ALLOWED_KEYS` limits the application side independently.
It has no permissive default: the deployment must provide the positive list.
`ADMIN_FLEET_CONFIG_RESTART_ENABLED=true` (or a visible
`CONFIG_SYNC_RESTART=true`) is also required before the Admin enables Publish.
The effective writable set is therefore the intersection of framework support,
the Admin publisher allowlist, and the node bootstrap delegation.

Grant the application role `s3:GetObject` and `s3:PutObject` only for the exact
override object. (`HeadObject` is covered by `s3:GetObject`; there is no
separate `s3:HeadObject` IAM action.) For an SSE-KMS object, the KMS key policy
also needs `kms:GenerateDataKey` for publication and `kms:Decrypt` for the
Admin status read. The node role needs read access to that object and KMS
decrypt access. Keep base-object publication and bootstrap settings outside
the Admin role.
Enable S3 versioning for recovery. Admin updates use the current ETag as an
`If-Match` precondition (and `If-None-Match: *` for the first write), so two
superusers cannot silently overwrite one another.
The initial delegation is:

```ini
CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS=GEOIP_PRIMARY_PROVIDER,GEOIP_FALLBACK_PROVIDER,GEOIP_ADDITIONAL_PROVIDERS,GEOIP_MOJO_PROVIDER_URL,GEOIP_MOJO_SYNC_ENABLED
```

Adding a future Admin-managed fleet setting requires explicit server-owned
registration: a `Descriptor(storage="fleet_config", writable="fleet_config")`,
a typed validator/default in `mojo.deploy.config_override`, and both deployment
allowlists. The Settings browser renders the descriptor; it has no independent
key allowlist. Secrets, imports/code expressions, and bootstrap keys are never
valid override values.

## Restarts are jittered by hostname

Every node polls the same bucket on the same timer, so an un-jittered restart
takes the whole fleet out simultaneously the moment a config lands. The delay
(0–59s) is derived from the hostname rather than randomly, so a given node's
slot is stable across runs and reproducible when you are working out what
happened.

After the delay, config sync uses `systemctl --no-block restart`. This is a
real systemd transaction enqueue, not a claim that the application has already
started. A synchronous restart from the boot-time oneshot can deadlock against
the unit's `Before=mojo-asgi.service` ordering. Enqueue refusal makes config
sync fail immediately; a later application start failure remains visible in
the target unit's systemd status/journal and in health checks.

## systemd

```ini
# /etc/systemd/system/config-sync.service
[Unit]
Description=Pull application config from S3
Documentation=https://github.com/... (django-mojo docs/django_developer/deploy)
Before=mojo-asgi.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m mojo.deploy.config_sync
```

```ini
# /etc/systemd/system/config-sync.timer
[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
```

```bash
systemctl enable --now config-sync.service config-sync.timer
```

`config_sync` creates `dirname(--target)` itself, so ordering it
`Before=mojo-asgi.service` cannot fail merely because `mojo-asgi`'s
`ExecStartPre` has not yet made `/opt/api/var`.

## If `config_sync` is broken

Because django-mojo is upgraded unpinned, a bad release reaches every node.
Two things limit the blast radius:

1. **Existing nodes keep serving.** `config_sync` never replaces a good config
   with a bad one or with nothing — every failure path leaves `django.conf`
   exactly as it was. A broken release means nodes stop *updating* their
   config; it does not mean they stop running. New nodes launched from an AMI
   with no baked config are the ones that hurt.
2. **Pin on one node first.** `pip install 'django-mojo==<last good>'` on a
   single box, `systemctl start config-sync.service`, and read the journal —
   every run logs the django-mojo version it came from, so a bad release is
   identifiable from `journalctl -u config-sync` without SSHing in to ask pip.

`--dry-run` reports what would happen and changes nothing; `--verbose` also
logs the no-op path (and keeps boto's wire logging at WARNING, so DEBUG does
not bury the one line you turned it on to read).

---

# `check_setup`

A read-only audit of an AWS account. Every call is a `Describe`/`Get`/`List`, so
it is safe against production.

```bash
python3 -m mojo.deploy.check_setup                       # /opt/api/var/django.conf
python3 -m mojo.deploy.check_setup --config ./var/django.conf
python3 -m mojo.deploy.check_setup --json                # for CI
python3 -m mojo.deploy.check_setup --section rds,cache
python3 -m mojo.deploy.check_setup --profile prod
python3 -m mojo.deploy.check_setup --topology reference
```

`--config` defaults to `/opt/api/var/django.conf` — an absolute path, not one
derived from `__file__`, which inside a wheel would resolve to a
`site-packages/var/django.conf` that never exists.

## Statuses and exit code

| Status | Meaning |
|---|---|
| `PASS` | no gap found |
| `WARN` | works, but is a known way to lose an afternoon later |
| `FAIL` | a correctness, durability or security gap that should be scheduled |
| `INFO` | context, no judgement |
| `BLIND` | the audit credential could not see this at all |

**Exit code is 1 if anything FAILed *or* anything came back BLIND.** BLIND
counts on purpose. Previously an `AccessDenied` was downgraded to a note and the
run still exited 0, so an audit key without `iam:List*` reported a clean IAM
section it had never read — a gate that returns green when it is blind is worse
than no gate. Grant the credential read access, or use `--section` to scope the
run to what it is meant to see.

## Default-on vs opt-in findings

**Universal — always checked.** True for any deployment: IMDSv2 required, S3
public-access blocks, public bucket policies, bucket versioning and default
encryption, unencrypted EBS and RDS storage, world-open SSH/Postgres/Redis,
publicly-accessible databases, CloudTrail, GuardDuty, `AdministratorAccess` on
the app key, access-key age, MFA.

**Topology — off by default.** Assertions about one specific deployment shape:
more than one EC2 node, more than one AZ, an Aurora reader, ≥7 day backup
retention, EBS snapshots present, application log groups present. A single-node
dev account is a different deployment, not a misconfigured one — shipping these
on by default from framework code produces a wall of FAILs that everyone learns
to ignore.

Turn them on per-run with `--topology reference`, or persistently with
`MOJO_DEPLOY_TOPOLOGY=reference` in `django.conf`. The JSON report records
which was in force.

The reference topology being asserted: a web node plus N API nodes behind a
load balancer, spread across ≥2 AZs; Aurora PostgreSQL writer + reader,
Multi-AZ; an ElastiCache replication group with automatic failover; one security
group per tier referencing each other by group id; CloudWatch alarms → SNS →
django-mojo's `/api/aws/cloudwatch/sns/alarm` ingest.

## Not the same tool as `aws-check`

See [aws/aws_check.md](../aws/aws_check.md#not-the-same-as-python--m-mojodeploycheck_setup).
Short version: `aws-check` is Django-aware, scoped to this deployment's own
resources, and can create what is missing; `check_setup` is account-wide,
read-only, and needs no Django at all. `check_setup`'s `observability` and S3
sections are deliberately reduced to what `aws-check` does *not* cover.

## Known cleanup

`check_s3` still reports a missing `AWS_CERT_BUCKET`. That finding predates
`certbot_sync` moving into this package and should be reconciled with it.

---

# `certbot_sync`

The fleet certificate plane: ONE node renews (decided by hostname against
`PRIMARY_BALANCER_HOST`), pushes the full lineage (fullchain + privkey +
chain + cert, always together) to `AWS_CERT_BUCKET`; every replica pulls when
S3 is newer, verifies the downloaded key matches the downloaded cert BEFORE
installing, stages next to `/etc/letsencrypt` (never `/tmp` — tmpfs makes
`os.replace` raise EXDEV), and reloads nginx behind an `nginx -t` gate.

Config comes from `${PROJ_PATH}/var/django.conf` (`--config` overrides; the
default derives from the `PROJ_PATH` env var, `/opt/api` absent). With
`AWS_CERT_BUCKET` / `LOAD_BALANCER_DOMAIN` unset it logs one line and exits
0, so the cron installs unconditionally and stays dormant on a single-node
box. `--renew` is the daily certbot run, role-aware: unconfigured boxes renew
themselves, replicas skip (certbot against a synced lineage corrupts it), the
primary renews then pushes in the same run. Full semantics are the module
docstring — the packaged copy is the canonical descendant of the skeleton's
`aws/certbot_sync.py` (#1599) and the semantics were ported verbatim.

The packaged cron templates (`1_certbot`, `4_certbot_sync`) invoke it as
`python3 -m mojo.deploy.certbot_sync --config @PROJ_PATH@/var/django.conf` —
`--config` explicit because cron's environment carries no `PROJ_PATH`.

---

# The node scripts: `update.sh` and `post_deploy.sh`

Both ship as packaged bash under `mojo/deploy/scripts/` and are executed
through a three-line **shim** each project keeps at `aws/update.sh` /
`aws/post_deploy.sh`. The shim is the project's only copy; the body lives in
the framework and moves with every `pip install`, so a fix reaches every
project on its next deploy instead of dying in a template nobody re-clones.

`update.sh` is the fleet update entry (see `docs/django_developer/edge/deploy.md`
for the orchestrator contract): deploy mode checks out the named sha,
installs the pinned framework, reports `deploy_status`, and on a canary
release failure reports **before** rolling back; `--manual` is the hands-on path;
bare invocation is refused. Deploy mode publishes the installed commit and
attempt UUID as one atomic `var/deploy_identity.json` before its v2 success
callback; the old `deploy_sha` / `deployment_uuid` pair is read only as a
bounded one-generation bridge. `post_deploy.sh` is the convergence pass update.sh
sudo-runs: project deps first, framework last, `migrate_locked` only under
`--migrate`, collectstatic, **render** (below), nginx top-level + `sec.d` +
`conf.d` (`.example` excluded, copied count logged), `node_retired.conf`
processing, `nginx -t` gate + reload, systemd + cron install from
`var/deploy/`, the structural stale-cron sweep, `var/logs` ownership, restart,
and a `PROBE_URL` health gate.

The rollback boundary is deliberately narrow. Dependency/framework install,
migrations, collectstatic, render, the app's nginx/systemd contract, nginx
validation/reload, `mojo-asgi` restart, `PROBE_URL`, and `sanity_check` are
release-critical: failure means the candidate cannot be trusted to serve and
the canary rolls back. MojoSec, security include refresh, auxiliary services,
timers, cron, retired-name cleanup and log ownership are housekeeping: failure
files a level-5 `edge_deploy` incident with a fixed phase and continues. Raw
command output never enters the incident. A deployment identity/callback
failure stops fleet progression and leaves the healthy canary in place so the
still-running parent job can record it; bookkeeping never rolls healthy code
back.

Mid-run self-replacement is designed in: the `pip install` inside post_deploy
swaps both packaged files for new inodes, but the executing bash keeps its fd
on the old copy, so the in-flight run completes on the code it started with
and the NEXT run resolves the new copy through `locate`.

The identity boundary is deliberately stricter than the code boundary. Before
mutating code, `update.sh` snapshots the last coherent identity and publishes
an invalidation marker. Success atomically renames the canonical v2 manifest:

```json
{"schema":2,"sha":"<full 40-hex live HEAD>","deployment":"<canonical uuid>"}
```

The manifest must exist before the success callback, which carries
`MOJO_DEPLOY_IDENTITY_READY=2`. A present-but-invalid manifest fails closed and
never borrows the legacy pair. A real application rollback restores the
snapshot only after reset, framework convergence, and sanity all succeed; an
incomplete rollback leaves no manifest and retains the invalidation marker.
If identity publication or its callback fails after a healthy candidate is
serving, the attempt remains unproven and fleet progression stops, but the
candidate is not rolled back. A delivery already on the
requested SHA/framework with a different attempt UUID still publishes that
fresh UUID, performs its sanity/callback path, and reaches the normal
jobman-last tail. Only the same SHA/framework/UUID is a true duplicate no-op.

## The shim contract

`aws/post_deploy.sh` in a project is exactly this (the canonical shim — the
comment line is where project deltas go):

```bash
#!/bin/bash
# project deltas, e.g.: export SANITY_URL="http://127.0.0.1:8080/api/version"
target="$(python3 -m mojo.deploy locate post_deploy.sh)" \
    || target="${PROJ_PATH:-/opt/api}/var/deploy/post_deploy.sh"
[ -f "$target" ] || { echo "FATAL: django-mojo is not installed and no snapshot exists — cannot run post_deploy (provisioning installs the framework first)" >&2; exit 1; }
exec bash "$target" "$@"
```

### `stage1.sh` is deliberately NOT locatable

`LOCATABLE` is `("update.sh", "post_deploy.sh")` and nothing else. That tuple
is a sudo-execution guard: a project shim runs whatever `locate` prints, as
root, so the set of names it will resolve is the set of things a shim may be
talked into executing. Keeping it at two is the whole control.

`stage1.sh` is never reached that way. It is published to the config bucket by
`provision apply` and downloaded by a booting node with its instance role, so
the provisioner resolves it by package path
(`storage.scripts_dir() + "/stage1.sh"`) instead. Adding it to `LOCATABLE`
would widen the oracle for a path that does not use it.

`aws/update.sh` is the same shape **without** the `var/deploy` fallback —
locate-or-FATAL. Python entry points that a project wants to keep at their
historical paths (`aws/certbot_sync.py`, `aws/check_node.py`) shim as:

```python
#!/usr/bin/env python3
import sys
try:
    from mojo.deploy.certbot_sync import main
except ImportError as err:
    print(f"FATAL: django-mojo is not installed — {err}", file=sys.stderr)
    sys.exit(1)
sys.exit(main(sys.argv[1:]))
```

Three properties are load-bearing:

- **Fail loud, mutate nothing.** With the framework missing, every shim
  prints one FATAL line and exits 1 *before* any flock, git or /etc mutation.
- **The snapshot fallback is post_deploy's alone.** Every successful
  post_deploy run snapshots its executing copy to
  `${PROJ_PATH}/var/deploy/post_deploy.sh`. That is the rollback self-heal:
  a canary rollback may pip-install a framework so old that `locate` cannot
  resolve, and the rollback still needs a post_deploy to converge with. No
  other entry point gets the fallback — a stale `update.sh` re-entering the
  fleet plane is a hazard, a stale post_deploy converging a rollback is the
  point.
- **Bootstrap ordering.** Provisioning installs django-mojo before the first
  shim run — the FATAL above is the guard, not a path to work around.

### `aws/update.sh` must be committed executable

The fleet deploy plane `exec()`s the configured `EDGE_DEPLOY_SCRIPT` path
directly, so `aws/update.sh` needs the execute bit **in git**, not just on the
node — a local `chmod` is lost on the next clean checkout, and the shim then
refuses the deploy on every node in the fleet at the same moment:

```bash
git update-index --chmod=+x aws/update.sh && commit the mode
```

`deploy_node` probes this before it starts anything and files a level-7
incident naming the path and this cure; `check_node`'s **shims** section audits
it (WARN, FAIL under `--require-shims`). `aws/post_deploy.sh` is exempt —
`update.sh` invokes it as `sudo bash <path>`, which needs no execute bit.

### A fork must declare the argv contract (a shim never has to)

`update.sh` carries a marker line naming the argv contract it speaks:

```bash
# mojo-deploy-contract: 2     # == mojo.apps.edge.services.deploy.DEPLOY_CONTRACT
```

and answers it directly, before the `cd`, touching nothing:

```bash
aws/update.sh --contract      # prints 2, exits 0 (works even with no PROJ_PATH)
```

`deploy_node` **reads** the configured script before exec'ing it. A shim always
passes — it references `mojo.deploy`, so its body is whatever the installed
framework ships and it cannot drift. A **fork** must carry either the marker or
every required flag (`--sha`, `--framework`, `--deployment`); a fork that parses
argv without them is refused by name, with a level-7 incident, before the deploy
starts. Anything the guard cannot read confidently — an unreadable file, a
`"$@"` forwarder, a wrapper that never mentions `--sha` — proceeds. Full ladder
and the release skew rule: `docs/django_developer/edge/deploy.md`.

### Shims can be one framework generation behind

`update.sh` `pip install`s the new framework *inside* the run, replacing both
packaged scripts with new inodes. The executing bash keeps its fd on the old
copy, so **the in-flight deploy completes on the code it started with** and
only the NEXT run resolves the new body through `locate`. That is deliberate
(mid-run self-replacement would be far worse), but it means a fix to
`update.sh` or `post_deploy.sh` reaches a node on the deploy *after* the one
that installs it. When triaging a node script, check which framework version
was installed **before** the run, not after.

## Project inputs (exported in the shim)

| Variable | Consumed by | Default | Meaning |
|---|---|---|---|
| `PROJ_PATH` | both scripts, locate fallback, certbot_sync | `/opt/api` | The deployed tree |
| `SANITY_URL` | `update.sh` | `http://127.0.0.1/api/version` | Passed as `--url` to **every** `sanity_check` (canary and rollback) |
| `PROBE_URL` | `post_deploy.sh` | `http://127.0.0.1/api/version` | The post-restart curl gate. Point it at a vhost that proxies straight to the asgi socket — a port-80 server that 301s everything false-passes `curl -f` |
| `APP_USER` | `post_deploy.sh` → `@APP_USER@` | `ec2-user` | Owns the tree, runs the job engines |
| `WEB_USER` | `post_deploy.sh` → `@WEB_USER@` | `www` | Runs the asgi app behind nginx |
| `ASGI_WORKERS` | `post_deploy.sh` → `@WORKERS@` | `4` | uvicorn worker count in `mojo-asgi.service` |
| `NGINX_ETC` / `SYSTEMD_ETC` / `CRON_ETC` / `LOGROTATE_ETC` | `post_deploy.sh` | `/etc/nginx` / `/etc/systemd/system` / `/etc/cron.d` / `/etc/logrotate.d` | Test seams — prod defaults, overridden only by harnesses |
| `MOJOSEC_ETC` / `MOJOSEC_STABLE_HELPER` | `post_deploy.sh` | `/etc/mojosec` / `/usr/local/lib/mojosec/mojosec_changes.py` | Test seams for the enrollment probe and the root-owned trusted-change helper — prod defaults, overridden only by harnesses |
| `CERTBOT_SYNC_LOCK` | `certbot_sync` | `/var/run/certbot_sync.lock` | Lock path override, exists for harnesses |

### `PROBE_URL` / `SANITY_URL` on an edge-converged node

Both default to `http://127.0.0.1/api/version`, and on a node the edge plane
has converged **that default cannot answer**. The generated vhosts claim
`server_name`, and the port-80 catch-all returns `444` (connection closed, no
response) to anything that does not match one. `curl -f` sees a hang-up, not a
success:

```
FATAL: app did not answer http://127.0.0.1/api/version within 30s of restart
```

Export a **vhost-true** URL from the shim — one whose `Host` matches a real
server block, or a port that proxies straight to the asgi socket:

```bash
export PROBE_URL="http://127.0.0.1:8080/api/version"
export SANITY_URL="http://127.0.0.1:8080/api/version"
```

The consequence is not a cosmetic warning. `post_deploy.sh` dies, so
`update.sh` treats the deploy as failed and **rolls the node back** to the
previous commit — on a canary, that fails the whole fleet deploy for a release
that was fine. Set both: `SANITY_URL` is passed to every `sanity_check`,
including the one inside the rollback.

### The asgi socket contradiction

There is a live tension between two documented positions and it is worth
naming rather than rediscovering: the `mojo-asgi` unit can be configured to
listen on a **unix socket** (nginx proxies to it), while `PROBE_URL` /
`SANITY_URL` are **HTTP URLs** curl must be able to fetch. On a socket-only
node there is no TCP port for the default probe to hit at all, and no amount of
`PROBE_URL` tuning invents one. Two supported cures, pick one per project:

1. **Give the probe a vhost.** Keep the socket, and point `PROBE_URL` at a
   `server_name`-matching URL served by nginx that proxies to that socket. The
   probe then tests the real serving path, which is the stronger check.
2. **Give the app a loopback port.** Configure `mojo-asgi` to bind
   `127.0.0.1:<port>` alongside (or instead of) the socket, and point
   `PROBE_URL` / `SANITY_URL` at it. This bypasses nginx, so a working probe no
   longer proves the vhost is right.

Neither is defaulted, because the right answer depends on whether the project
wants the probe to cover nginx. What is *not* supported is leaving the default
in place on a converged node.

---

# Templates, `render`, and `var/deploy`

The framework ships the node's cron and systemd contract as templates:

```
mojo/deploy/templates/cron.d/    1_certbot  2_mojo_cron  3_mojo_jobs  4_certbot_sync
mojo/deploy/templates/systemd/   mojo-asgi.service  config-sync.service  config-sync.timer
```

`python3 -m mojo.deploy render --dest ${PROJ_PATH}/var/deploy --project-path P
--app-user U --web-user W --workers N` substitutes `@PROJ_PATH@`,
`@APP_USER@`, `@WEB_USER@`, `@WORKERS@` (plain string replacement, no
engine), writes the results 0644 into `<dest>/{cron.d,systemd}`, then overlays
the project's own `aws/cron.d/` and `aws/nginx/systemd/` files verbatim. It
dies loudly on an unwritable dest or any placeholder left unsubstituted.
`post_deploy` requires the rendered `mojo-asgi.service`; without it the web app
cannot start. An empty cron set is a housekeeping warning, never an application
rollback.

On a root, production-shaped `${PROJ_PATH}/var/deploy` invocation, `render`
also converges nginx's persistent spill contract before it writes templates.
This placement is intentional: an upgrading `post_deploy.sh` continues running
its old inode after pip replaces the package, but its unchanged
`python3 -m mojo.deploy render ...` argv imports the newly installed module.
The first release carrying a repair therefore fixes the node before the old
shell reaches MojoSec, `nginx -t`, or reload; it does not wait for a later Edge
generation.

The contract is `/var/lib/django-mojo/nginx/` with private worker-owned `0700`
leaves for `client_body`, `proxy`, `fastcgi`, `uwsgi`, and `scgi`, plus the
root-owned `/etc/nginx/conf.d/00_django_mojo_runtime.conf`. The active
`nginx -T` worker user must equal `WEB_USER`. Deployment applies a durable
nginx-writable SELinux label where enforcing, drops to the actual worker uid
and gid to create/unlink a sentinel in every leaf, and requires every active
temp directive exactly once before continuing. The tree and global fragment
survive framework rollback, so an older renderer cannot recreate the outage.
Do not replace this with `/tmp` or a package-owned `/var/lib/nginx` directory.

`var/deploy/` is the single source of truth for what the last deploy shipped:
post_deploy installs `/etc` copies FROM it, and check_node byte-compares
`/etc` AGAINST it. The template names deliberately match the historical
provisioning heredocs (`1_certbot`, `2_mojo_cron`, …), so on a node that
predates the package the first converge overwrites the plain copies with the
gated ones instead of installing duplicates alongside them.

Every cron template keeps a `@PROJ_PATH@` reference on its job line (the log
redirect does this naturally), which is what lets post_deploy's structural
sweep recognize installed copies as project-owned.

## Collision policy — fixes propagate by default

A project file in `aws/cron.d/` or `aws/nginx/systemd/` whose **name**
collides with a framework template is **inert**: render logs a loud warning,
skips it, and the framework copy wins. To keep a deliberate fork, declare the
name in `${PROJ_PATH}/aws/node_overrides.conf` (one name per line, `#`
comments) — then the project copy replaces the framework render.
check_node reports an undeclared collision WARN (FAIL under
`--require-shims`). Non-colliding names copy through untouched — extras are
always the project's to add.

## `node_retired.conf` — declaring what to remove

`${PROJ_PATH}/aws/node_retired.conf` (optional) lists names the project once
shipped and has since retired, one per line: `cron.d/<name>` or
`conf.d/<name>`, `#` comments. post_deploy removes each with an explicit
logged `rm`; check_node FAILs a declared cron name still installed and WARNs
a declared vhost. This is the explicit-list complement of the structural
sweep, for the two cases discovery cannot cover: cron files that never
mention `PROJ_PATH`, and nginx `conf.d` (where a mentions-the-project rule
would delete live node-managed vhosts). It lives *outside* `aws/cron.d/` so
the convergence glob can never copy the list itself into `/etc`.

---

# `check_node`

The complement of `check_setup`: audits one node, never calls an AWS API,
mutates nothing. Sections: `repo`, `framework`, `cron`, `systemd`, `nginx`,
`certs`, `config_plane`, `shims`, `legacy`, `var_ownership`, `jobs`. Exit 1
iff anything FAILed.

The nginx section audits the same five-path runtime contract, worker identity,
metadata, SELinux accessibility, active `nginx -T` directives, and a real
worker create/unlink probe. Repair is the ordinary automated deployment. An
emergency per-instance ownership change is break-glass debt: record the node
and reason, then keep the incident open until a deployment converges and
`check_node --section nginx` proves the durable contract.

```bash
python3 -m mojo.deploy.check_node                      # on the node
python3 -m mojo.deploy.check_node --ssh api1           # from anywhere
python3 -m mojo.deploy.check_node --json --require-shims
python3 -m mojo.deploy.check_node --probe-url http://127.0.0.1:8080/api/version
```

What moved when it left mverify: the cron/systemd contract source is
`${PROJ_PATH}/var/deploy/{cron.d,systemd}` (absent → INFO with a
"run post_deploy" hint, never FAIL); the probe URL and the `ec2-user`/`www`
identities are `--probe-url` / `--app-user` / `--web-user`; the hardcoded
retired-name lists are read from `aws/node_retired.conf`; `--repo-path`
(nginx contract tree) defaults to `--project-path`.

The **shims** section is new: each of `aws/update.sh`, `aws/post_deploy.sh`,
`aws/certbot_sync.py`, `aws/check_node.py` under `${PROJ_PATH}` grades PASS
when it references `mojo.deploy`, WARN when it exists without the reference
(a fork cut off from framework fixes — FAIL under `--require-shims`), INFO
when absent. `aws/update.sh` additionally has its **mode** audited: the deploy
plane exec()s that path, so a shim without the execute bit grades the same
WARN/FAIL and carries the `git update-index --chmod=+x` fix (see above).
Undeclared template-name collisions grade the same way. Locally
(not over `--ssh`) it also renders the installed package's templates in
memory and diffs them against `var/deploy/` — a difference is INFO:
"framework templates moved since the last deploy — run post_deploy". The
last-deploy compare against `/etc` stays the primary audit; the freshness
diff only says the *next* deploy will change things.

`var/django.conf` and `var/bootstrap.conf` are never read — stat + key-NAME
presence grep only. Do not widen that.

---

# Downstream adoption

Order matters only in that the framework release carrying this package must
be installed before the shims land:

1. **django-mojo-skeleton** — replace `aws/update.sh`, `aws/post_deploy.sh`,
   `aws/certbot_sync.py` with shims (skeleton defaults need no exports);
   delete the provisioning cron/systemd heredocs in favor of
   `render` + post_deploy convergence.
2. **mverify_api** — shim with
   `export SANITY_URL="http://127.0.0.1:8080/api/version"` and
   `export PROBE_URL="http://127.0.0.1:8080/api/version"` (its port-80 vhost
   301s everything); move `api.mojoverify.com.conf`, `setup.conf` and the
   legacy cron names (`2certbot` et al) into `aws/node_retired.conf`; delete
   its forked `check_node.py`/`certbot_sync.py` bodies.
3. **maestro api** — pins released django-mojo; adopts the same shims on its
   next framework bump.

A project's first converged deploy after adopting: the shim locates the
packaged post_deploy, render fills `var/deploy/`, the gated cron templates
overwrite the provisioning-era copies by name, and
`python3 -m mojo.deploy.check_node --require-shims` goes green.

---

# `jobman`

Controls the **foreground** job engine and scheduler on one node.

```bash
python3 -m mojo.deploy.jobman status                  # both components
python3 -m mojo.deploy.jobman start engine
python3 -m mojo.deploy.jobman stop --root /opt/api
```

## Two process planes, and why they stay separate

| | `mojo.deploy.jobman` | `python -m mojo.apps.jobs.cli` |
|---|---|---|
| Mode | foreground | daemon |
| Pidfiles | `<root>/var/pids/job_engine.pid` | `/tmp/job-engine-<runner_id>.pid` |
| Started by | the every-minute `3_mojo_jobs` cron | an operator |
| Needs | nothing configured | Redis **and** Postgres, on every command |

Nothing was migrated, and nothing should be. `jobman stop` will not touch a
daemon-mode engine and the jobs CLI will not see anything jobman started.

Two things make the jobs CLI unable to host this even if the planes were the
same. `mojo/apps/jobs/cli.py` runs `validate_environment()` on every command
path *including `status`* — it pings Redis, opens a database cursor and counts
rows, and a node-status tool has to answer on a box where nothing is configured
yet. And importing anything under `mojo.apps.jobs` with no
`DJANGO_SETTINGS_MODULE` raises `AttributeError` from the `mojo.helpers.logit`
chain, so the package cannot even be loaded here.

## The status contract

mverify's `check_node.py` runs `jobman status 2>&1` and matches lines with
`startswith("Engine running")` / `"Scheduler running"`. Four properties follow,
and all four are asserted by `tests/test_deploy/jobman.py`:

- **stdout only, stderr empty.** The consumer merges the two streams, so a stray
  warning would land in the middle of what it parses.
- **the status line comes first**, then any detection line.
- **a bare run prints both components**, Engine first.
- **the exit code is always 0.** `check_node.py` reads a non-zero rc as "jobman
  unavailable" and stops looking at the output.

| Line | Meaning |
|---|---|
| `Engine running (PID 1234)` | the pidfile's PID is alive |
| `Engine not running (stale PID file: /opt/api/var/pids/job_engine.pid)` | a pidfile exists, its PID does not |
| `Engine not running` | no pidfile, nothing matching |
| `Engine extra instances detected: 2000 3000` | strictly MORE than the one instance jobman manages |
| `Engine unmanaged instances detected: 2000` | a jobs process runs, but no live pidfile tracks it |

"extra" means *strictly more*. On a healthy node the pidfile's own PID always
turns up in the pgrep result, so it is subtracted before deciding which line
prints — without that, every healthy node reports the engine as a duplicate of
itself.

## Pidfiles, logs, and the pgrep pattern

| Thing | Path |
|---|---|
| pidfile | `<root>/var/pids/job_<component>.pid` |
| log | `<root>/var/logs/job_<component>.log` |
| runner | `<root>/bin/jobs.py` (override with `--runner`) |

The root resolves `--root` → `$MOJO_PROJECT_ROOT` → the working directory, and
is made absolute — the stale-pidfile line prints it, and a relative path there
means something different to every reader. `var/logs` and `var/pids` are created
at startup.

Process matching shells out to `pgrep -f` / `ps -p` rather than re-deriving the
scan in Python, so the deployed matching semantics carry over unchanged. The
pattern is the runner's path **relative to the root**:

```
bin/jobs\.py engine foreground
```

which matches a relative spawn and an absolute one alike, so old and new
processes co-match during a rollout. Only the path is `re.escape`'d — escaping
the whole string would escape the spaces too. A `--runner` resolving *outside*
the root is refused (exit 1): the `../`-shaped pattern it would produce can
never match a command line, so `status` would report not-running forever while
the cron spawned a fresh duplicate every minute.

**Every PID is a string.** A pidfile holding junk goes straight to `ps -p` and
reports not-running; nothing calls `int()` on a pidfile.

## The project-side shim

`django-mojo-skeleton/bin/jobman` becomes a wrapper, so `./bin/jobman status`
keeps working and every project picks up fixes through pip:

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export DJANGO_SETTINGS_MODULE=settings
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
exec python3 -m mojo.deploy.jobman --root "$ROOT" "$@"
```

Output is byte-identical to the script it replaces with **one deliberate
exception**: a missing or non-executable runner is one stderr line and exit 1
here, where bash printed a success line and returned 0 — so a cron reported a
healthy start every minute while nothing ran.

---

# MojoSec node deployment

`post_deploy.sh` invokes the just-installed package directly (after `pip`, even
on the first framework upgrade). It never installs a root unit from the
group-writable project tree or `var/deploy`:

```bash
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec converge \
  --mode enrolled --criticality enrolled)
```

MojoSec observe mode requires Python 3.11 or newer at `/usr/bin/python3` (`-P`
safe-path support). AL2023 satisfies this contract; an AL2 image must provision
Python 3.11+ before enrollment. A legacy Python 3.10 node may still converge
off or retire an absent old service during an ordinary framework upgrade.
`-E` ignores `PYTHON*` injection and `-P` removes the
current directory from imports while retaining AL2023's root-pip
`/usr/local/lib/python3.x/site-packages`. The unit also runs from root-owned
`/`, clears Python path/home variables, uses `ProtectHome=tmpfs`, and exposes
only exact read-only binds for approved root and `ec2-user` persistence paths;
unrelated home content stays hidden. `check_node` probes that live namespace and
rejects a Mojo package outside a root-owned, non-writable system
site. Do not substitute `-I` or `-s`: on AL2023 both hide the root-pip package.

The packaged script passes `--mode enrolled --criticality enrolled`: root-only
`/etc/mojosec/enrollment.json` persistently selects `off`/`observe` and
`best_effort`/`required` across every ordinary deploy. With no enrollment it
resolves to off/best-effort, so upgrading a legacy node adds no noisy log.
`MOJOSEC_MODE` and `MOJOSEC_DEPLOY_CRITICALITY` remain explicit emergency/test
overrides, not a persistent fleet source. `required`
means local convergence (config, nginx validation/reload, and service
lifecycle) must succeed; it does not require the receiver to be reachable,
because delivery failures spool durably. `best_effort` returns a visible
warning and leaves an unenrolled/invalid node operationally unchanged.

The app-managed file is desired policy, not privileged runtime authority. Root
convergence reads it with `O_NOFOLLOW`, rejects protected fields, merges the
root-only host enrollment, validates all bounds, and atomically materializes
the only service-readable config. A changed effective hash restarts the active
sensor; invalid policy keeps the prior canonical file and service.

| Material | Contract |
|---|---|
| `/opt/api/var/mojosec.json` | app-managed, nonsecret desired collectors/FIM/batching policy; never read by the root service |
| `/etc/mojosec/enrollment.json` | root:root 0600 host identity, exact HTTPS receiver, nginx plane/trusted proxies, and allowed FIM roots |
| `/etc/mojosec/config.json` | root:root 0600 generated canonical runtime config; never hand-edit |
| `/etc/mojosec/credential` | root:root 0600 per-installation API key |
| `/var/lib/mojosec` | root:root 0700 durable SQLite spool; retained across off/rollback |
| `/run/mojosec/status.json` | root:root 0640 bounded health/provenance snapshot; inspect through `sudo check_node` |
| `/etc/mojosec/expected_changes.json` | optional root:root 0600 v2 exact FIM annotations; v1 remains readable during rollout |
| `/etc/systemd/system/mojosec.service` | root-owned packaged unit, Python safe-path launch, runs as root with systemd hardening |
| `/usr/local/lib/mojosec/mojosec_changes.py` | root-owned stable producer helper used before pip replaces site-packages |

The recommended desired policy sets `"profile":"al2023-web-v2"`. The profile
is packaged and immutable: fast host/config/home/cloud-init/local-library
coverage runs each minute, slow boot and system-binary coverage runs every six
hours, and RPM verification includes the isolated system Python's constrained
site-packages roots. It excludes `/opt/api`, `/opt/www`, and MojoSec's own
private/control state. Profile activation is an explicit
`baseline-preview` → `baseline-initialize --confirm-digest <digest>` ceremony;
ordinary service startup never blesses the first scan. `check_node` fails an
active digest without initialized fast, slow, and RPM baselines.

An enabled RPM tier additionally requires the AL2023 system `python3-rpm`
binding at the configured interpreter. One `-I` helper opens one read-only
`TransactionSet` for the scan, proves `RPMDBI_INSTFILENAMES` against an actual
installed file, and records the RPM database cookie. Exact-path responses are
bounded and structural: no installed owner keeps SHA-256 coverage, exactly one
validated NEVRA selects package verification, and multiple/invalid results fail
the tier. Non-installed file states never claim ownership. Index/header/DB
failure, helper death or timeout, protocol/output/query bounds, unexpected
stderr, or cookie drift keeps the prior baseline authoritative. There is no
localized `rpm -qf`, BASENAMES/PROVIDENAME, generic-exit, or per-path helper
fallback.

`al2023-web-v1` is retained only for existing baseline identity and rollback.
Do not select it for a new AL2023 baseline: its
`/var/lib/cloud/instance/scripts` target descends through cloud-init's mutable
`instance` symlink, which descriptor-safe traversal correctly refuses. V2
removes that redundant alias descendant and retains recursive content coverage
through `/var/lib/cloud/instances`. An existing v1 deployment stays on v1 until
an operator selects v2, records a complete preview, and explicitly initializes
the exact v2 digest; profile mismatch fails closed and never rebaselines.

Post-deploy, `node_setup`, and `certbot_sync` route monitored host changes
through the stable expected-change helper. The helper declares exact paths
before one child mutation, completes only on success, and aborts failure so the
sensor still reports those bytes as unexplained. Pip resolution is bounded and
uses incoming plus installed wheel `RECORD` paths, including exact installer
metadata and generated scripts/bytecode; it never diffs an arbitrary
site-packages scope. Repeated parent destinations are deduplicated before
bounds are enforced. Caller-declared host changes retain a 4,096-path ceiling;
package paths mechanically derived from bounded installer and wheel metadata
use a 65,536-path secondary corruption ceiling, with the 20 MiB serialized
state bound normally binding first. Journal and manifest state remain
root-only. The v2
manifest adds operation identity/kind/completion time. Events are durable
immediately and wait no more than 120 seconds for a late matching annotation.

**MojoSec control state is never journal scope, and never a deploy blocker.**
`/etc/mojosec`, `/var/lib/mojosec` and `/run/mojosec` — including the Audit
rollback record `/etc/mojosec/audit-state.json` — belong to the
`mojo.deploy.mojosec converge` transaction, which snapshots and restores them
itself. They are excluded from the integrity profile, so the journal has
nothing there to annotate, and the manifest the sensor trusts *lives* under
`/etc/mojosec`: a journal that could pre-authorize writes there could
pre-authorize tampering with the sensor's own trust anchors. `validate_paths`
therefore rejects those roots outright.

A caller-declared path under them is a different problem from a producer
deriving one. The 1.11.9 and 1.11.10 post-deploy bodies both declared
`/etc/mojosec/audit-state.json`, and the body that drives an upgrade is the
*previously installed* generation's — so refusing it wedged every enrolled node
before the converge child could run (item 2014). The `run` intake now drops
control-state paths with a visible warning: never journaled, never fatal.
Producer-derived paths and in-process callers ship in the same wheel, cannot
skew, and still fail closed.

Roll out the producer-capable package before selecting the integrity profile:
deploy the helper and exercise normal deploy, node setup, and certificate
operations while the profile remains inactive. Only then select
`al2023-web-v2`, preview all tiers, and initialize the exact previewed digest.
This keeps the first active integrity baseline from coinciding with a framework
upgrade that has no producer. A rollback either selects a retained initialized
profile digest or turns enrollment off; it never silently creates a replacement
baseline.

Install root enrollment and credentials only through stdin:

```bash
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec install-enrollment) < enrollment.json
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec rotate-credential) < credential.txt
```

Neither command prints the secret. For rotation, rotate the bearer centrally
with an overlap window first, install it on the host, then verify an accepted
delivery before revoking the old bearer. A failed live restart restores the old
credential and restarts it; it never prints `ok:true` after a failed restart.

MojoSec's nginx JSON log uses `escape=json` and retains bounded raw request URI,
referrer, user agent, host, method/status, upstream status/timing, request
timing, request ID, scheme/protocol/TLS, client/direct-peer/server ports,
request/response byte counts, upstream connect/header/response timing and
upstream response-length/received/sent bytes. It retains direct peer
(`$realip_remote_addr`) and resolved client (`$remote_addr`). It never includes
bodies, cookies, authorization, or
arbitrary headers. Only exact file-configured CIDRs render `set_real_ip_from`.
Both standard and Edge planes securely use the fixed root:root 0600
`/var/log/nginx/mojosec.json.log`; the standard plane installs the generated
fragment/snippet and wires it into `/etc/nginx/django.inc`, while Edge renders
the same shared format in its owned configuration. Sensor endpoints
must use unslashed `/api/incident/mojosec/batch`; nginx caps both exact slash
spellings at 512 KiB so a framework redirect/alias cannot bypass the wire cap.
Edge enrollment uses `nginx_plane=edge`. Its unprivileged staged `nginx -t`
copy disables access logs; the authoritative root validation checks the real
protected path. Edge `MOJOSEC_MODE=off` emits neither log nor route.

Canonical config, managed nginx files, the standard include, log metadata,
current/retired exact units, deploy state, and lifecycle form one transaction.
Every failed candidate restores the prior bytes/modes, runs `nginx -t`, and
reloads the restored graph. Observe must finish active+enabled; off must finish
inactive+disabled. Off removes the standard security logging graph but
preserves spool and credentials. OSSEC/Wazuh is never removed.

Logrotate keeps 14 compressed daily files and uses `copytruncate` so nginx
continues writing the securely precreated root:root 0600 inode. Rotated copies
stay root:root 0600; there is no nginx `USR1` reopen that
would chown the active file to its worker. `copytruncate` has an unavoidable
narrow copy/truncate race in which a line written by nginx can be absent from
both files; this is preferred to making the web worker owner of security
evidence, and the canary measures the gap under load. `maxsize 50M` is evaluated when the
system logrotate timer runs; it is not a continuous hard cap. `check_node --section mojosec
--mojosec-mode observe --mojosec-sensor-id <host-id>` uses sudo to inspect only
bounded status/enrollment projections and metadata; it never opens or prints
the credential, canonical config, SQLite spool, or FIM manifest. It audits
status freshness, core collectors, backlog/delivery, generated and active nginx
contracts, proxy CIDRs, log metadata, and config hashes. In observe mode it also
runs `python -m mojo.mojosec ... check`; an RPM-enabled profile must pass the
same isolated binding, transaction, database/header, and installed-file index
preflight before deployment readiness is green. The check never installs a
binding or opens the API-key credential. Default `auto` keeps legacy disabled
nodes informational.

## Canary and cleanup

Before enabling observe, create a separate protected `mojosec_ingest` API key,
install desired policy and a root enrollment containing
`"mode":"observe","criticality":"required"`, then install the credential and
run ordinary `post_deploy`. Do not run another deploy during a disposable
canary started with a temporary CLI/environment override; the next deploy
correctly returns to the enrolled lifecycle. Observe never bans locally.

For `al2023-web-v2`, use a disposable enrolled AL2023 node. First deploy the
producer-capable package with no profile selected and exercise its normal
deploy, node setup, and certificate paths. Then select the profile, record the
complete `baseline-preview`, and initialize only the digest it prints. Require
`check_node` to show initialized `fast`, `slow`, and `rpm` tiers, the expected
60-second/six-hour cadence, system-Python RPM/non-RPM coverage, and the exact
`ProtectHome=tmpfs` bind visibility before proceeding with the signal checks
below. Keep this canary as a release gate; automated tests do not replace it.

Record a UTC `CANARY_STARTED_AT`, baseline status/log inode+size and
`systemctl show mojosec.service -p MemoryCurrent -p CPUUsageNSec -p TasksCurrent`,
then exercise every collector:

1. Perform a benign SSH login/logout with the normal canary account and one
   harmless `sudo -n /usr/bin/true`; require the corresponding login/session
   and sudo evidence. On the security-admin Event, require the exact bounded
   command, actor, target user, executable path, cwd, TTY, boot ID, audit
   session, and a true truncation marker whenever the sensor retained only a
   prefix. Require `source_ip` plus `attribution="audit_session"` or `"who"`
   only when the proof tuple is complete; otherwise require explicit
   `attribution="none"` and a null source IP.
2. Under an explicitly enrolled canary FIM root, create a file, modify it,
   `chmod` it, and delete it. Require create/modify/delete evidence and no
   traversal outside that root.
3. Through the real nginx vhost request `/wp-login.php` and a canary-only route
   deliberately returning 503. Require a probe and a web-error event; routine
   2xx/ordinary 404 traffic must remain absent.
4. Make the central canary receiver return 503 for this installation key,
   generate another signal, and require `spooled_events` to rise without data
   loss. Restore the receiver, require authenticated acknowledgements and the
   spool to drain to baseline. `required` deployment itself does not test this.
5. Restart `mojosec.service`; require the same sensor identity, config hashes,
   durable cursor/FIM baseline, and backlog/delivery counters afterward.
6. Run `logrotate -f /etc/logrotate.d/mojosec`; require the active inode and
   root:root 0600 metadata to remain unchanged, its size to reset, and a
   subsequent probe line to appear without restarting/reopening nginx. Require
   every archive to be regular root:root with no world access, and require the
   collector cursor to reset and continue without a sustained error, malformed
   burst, or unexplained sequence gap. Also confirm the system logrotate timer
   is enabled; `maxsize 50M` is only checked when that timer/command runs.

The release gate is: no MojoSec FAIL from `check_node`; zero capacity-drop
counters; backlog returns to baseline; no sustained delivery/collector error;
strictly post-`CANARY_STARTED_AT` published `MojoSecReceipt` rows for the exact
sensor prove the probe, controlled 5xx, auth/sudo and FIM signals; no unrelated
tenant stamp; explainable event volume; log/spool disk growth within the
measured test volume; no FD/task leak; and after the burst the sensor stays
below the agreed canary budget (initial fleet gate: 150 MiB memory, 32 tasks,
and under 5% of one CPU over a five-minute idle window).

For an enrolled AL2023 release candidate, record the exact isolated RPM probe
as passing, require the RPM tier's `last_success` to become non-null after a
complete cycle, and require Audit health to remain green with `lost=0`, bounded
backlog, and no false provenance failure. The same gate must show zero spool
drops and at least one accepted signed delivery; unit tests do not substitute
for that live-node evidence.

Rollback persistently by reinstalling enrollment with `"mode":"off"` and
running ordinary `post_deploy` (or an enrolled converge). Confirm
inactive+disabled, nginx security logging absent, and legacy OSSEC still
present. Preserve `/var/lib/mojosec` and protected enrollment for diagnosis;
revoke the canary API key centrally. Only on a host explicitly being destroyed
may an operator remove `/var/lib/mojosec` and `/etc/mojosec` separately.

# `node_setup`

The reusable, non-cert third of a project's `ec2_deploy.sh`: three idempotent
actions, safe to re-run on every deploy.

```bash
sudo python3 -m mojo.deploy.node_setup --root /opt/api
python3 -m mojo.deploy.node_setup --dry-run          # plan only, no root
```

| Action | What it does |
|---|---|
| var dirs | create `var/{logs,pids,keys}`, chown `--owner`, `2775` on directories **including `var/` itself**, `0664` on regular files; reject every symlink/non-file and mutate only pinned `O_NOFOLLOW` descriptors |
| systemd | copy `*.service` **and** `*.timer` whose bytes differ, `daemon-reload` only when something changed, then `enable --now` the **timers** |
| cron | write `/etc/cron.d/3_mojo_jobs` (0644), whose user field is `--cron-user` |

Nothing is written when nothing differs, and a converged node says so:

```
node_setup: nothing to change
```

`--dry-run` prefixes every planned line with `would ` and changes nothing; it is
the way to inspect a node without touching it, and it works as any user. A real
run **requires root** — it writes under `/etc` — and refuses with one line
otherwise.

## `node_setup` on a node the portal added

The Admin capacity action that adds an EC2 node runs exactly this, from the
clone's cloud-init user-data, after `config-sync` and before restarting
`mojo-asgi` and the job engine:

```bash
python3 -m mojo.deploy.node_setup --root "$ADMIN_CAPACITY_NODE_ROOT" || true
```

Idempotence is what makes that safe: the clone boots from an AMI of a node
where `node_setup` had already converged, so on a healthy clone it prints
`nothing to change` and exits. It is there for the case where the source's
units or cron had drifted from what the installed django-mojo expects.

The restart that FOLLOWS it is the load-bearing part. Cloud-init runs late, so
the app service and the job engine already started under the source AMI's
hostname; the capacity user-data sets a unique hostname first and then restarts
both, because the hostname IS the runner id, the readiness node id, and the
certbot primary election. See
[aws/capacity.md](../aws/capacity.md).

## `--owner` is not `--cron-user`

| Flag | Default | Governs |
|---|---|---|
| `--owner` | `ec2-user:www` | ownership of `var/` **only** |
| `--cron-user` | `ec2-user` | the account the job engine runs as |

They are separate flags on purpose. Overloading one onto both means an
ownership fix — `--owner deploy:www` — silently changes which account runs the
engine on every node in the fleet. An owner that does not resolve on this box is
a warning, not a refusal: a wrong group is recoverable, a node with no
`var/pids` is not.

## Timers are enabled; services are not

`mojo-asgi` cannot start before `var/django.conf` exists, so it waits for the
operator. The sync timers are the opposite — they are what *fetches*
`django.conf`, and each no-ops harmlessly on an unconfigured box, so a fresh
node that never enabled them would look fully installed and never converge.
Copying only `*.service` is why `config-sync.timer` sat in every repo and was
never installed on any box.

Units are read from `--units-dir` (default `<root>/aws/nginx/systemd`). A
project with no such directory is a normal shape: quiet skip, exit 0.

## It writes no cert cron, ever

`node_setup` installs `3_mojo_jobs` and nothing else, and it removes no cron
file it did not write.

The `4_certbot_sync` pull tick and the gated `1_certbot` renew are a
**safety unit**. The pull is what creates a synced certificate lineage (regular
files, not certbot's symlinks into `archive/`), and an ungated `certbot renew`
against one corrupts it — so the hazard and its gate have to be installed by
the same run, or a node ends up with a puller and no gate. That pair now ships
as the packaged cron templates above, rendered and installed together by one
post_deploy convergence — never by `node_setup`.
`tests/test_deploy/node_setup.py` asserts no node_setup plan ever mentions
certbot.

---

## What stays in `django-mojo-skeleton`

`ec2_bootstrap.sh`, `ec2_deploy.sh` and the nginx vhost files stay. They are
the per-project deployment shape — which repo, which domains, which vhosts —
not framework behaviour, and a project is expected to edit them.
`update.sh`, `post_deploy.sh`, `certbot_sync.py` and `check_node.py` are the
opposite case: they must be identical everywhere, so their bodies live here
and projects keep only the shims.

What `ec2_deploy.sh` keeps owning: the snakeoil placeholder cert, nginx config
installation, project-specific pip extras, and the operator instructions it
prints at the end. Its var-dirs, systemd and jobs-cron blocks are what
`node_setup` replaces, and its cert-cron heredocs are superseded by the
packaged `1_certbot`/`4_certbot_sync` templates the first post_deploy converge
installs by the same names.
