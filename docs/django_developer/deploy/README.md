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
| `python3 -m mojo.deploy locate <name>` | Prints the absolute packaged path of `update.sh` / `post_deploy.sh` for the project shims |
| `python3 -m mojo.deploy render --dest …` | Materializes the packaged cron/systemd templates into `${PROJ_PATH}/var/deploy` |
| `mojo/deploy/scripts/update.sh` | The fleet update entry (deploy / manual modes) — packaged bash, run through a project shim |
| `mojo/deploy/scripts/post_deploy.sh` | Post-checkout convergence: deps → framework → migrate → render → nginx/systemd/cron → restart + probe |

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
```

MojoSec convergence snapshots every Audit rules source, generated and active
state before replacing the AL2023 `task,never` seed. Unknown operator rules or
concurrent changes stop deployment. Failed load/verification restores the
snapshot. The installed health timer owns `CAP_AUDIT_CONTROL`; the main sensor
does not. A root-owned stable helper lets the currently running deployment shim
restore the pre-feature rules when a release downgrades to a framework without
the feature. Broker-only units, sudoers and executable are then removed while
the intentionally retained legacy direct grants keep rollback operational.

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
s3://<AWS_CONFIG_BUCKET>/<AWS_CONFIG_PREFIX>/django.conf
    |
    v   python3 -m mojo.deploy.config_sync   (systemd oneshot at boot + timer)
/opt/api/var/django.conf   0600, owned by the app user
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

## Restarts are jittered by hostname

Every node polls the same bucket on the same timer, so an un-jittered restart
takes the whole fleet out simultaneously the moment a config lands. The delay
(0–59s) is derived from the hostname rather than randomly, so a given node's
slot is stable across runs and reproducible when you are working out what
happened.

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
failure reports **before** rolling back; `--manual` is the hands-on path;
bare invocation is refused. `post_deploy.sh` is the convergence pass update.sh
sudo-runs: project deps first, framework last, `migrate_locked` only under
`--migrate`, collectstatic, **render** (below), nginx top-level + `sec.d` +
`conf.d` (`.example` excluded, copied count logged), `node_retired.conf`
processing, `nginx -t` gate + reload, systemd + cron install from
`var/deploy/`, the structural stale-cron sweep, `var/logs` ownership, restart,
and a `PROBE_URL` health gate. It fails loudly at every step — a deploy that
half-worked and said nothing is worse than one that stops.

Mid-run self-replacement is designed in: the `pip install` inside post_deploy
swaps both packaged files for new inodes, but the executing bash keeps its fd
on the old copy, so the in-flight run completes on the code it started with
and the NEXT run resolves the new copy through `locate`.

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

## Project inputs (exported in the shim)

| Variable | Consumed by | Default | Meaning |
|---|---|---|---|
| `PROJ_PATH` | both scripts, locate fallback, certbot_sync | `/opt/api` | The deployed tree |
| `SANITY_URL` | `update.sh` | `http://127.0.0.1/api/version` | Passed as `--url` to **every** `sanity_check` (canary and rollback) |
| `PROBE_URL` | `post_deploy.sh` | `http://127.0.0.1/api/version` | The post-restart curl gate. Point it at a vhost that proxies straight to the asgi socket — a port-80 server that 301s everything false-passes `curl -f` |
| `APP_USER` | `post_deploy.sh` → `@APP_USER@` | `ec2-user` | Owns the tree, runs the job engines |
| `WEB_USER` | `post_deploy.sh` → `@WEB_USER@` | `www` | Runs the asgi app behind nginx |
| `ASGI_WORKERS` | `post_deploy.sh` → `@WORKERS@` | `4` | uvicorn worker count in `mojo-asgi.service` |
| `NGINX_ETC` / `SYSTEMD_ETC` / `CRON_ETC` | `post_deploy.sh` | `/etc/nginx` / `/etc/systemd/system` / `/etc/cron.d` | Test seams — prod defaults, overridden only by harnesses |
| `CERTBOT_SYNC_LOCK` | `certbot_sync` | `/var/run/certbot_sync.lock` | Lock path override, exists for harnesses |

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
dies loudly on an unwritable dest or any placeholder left unsubstituted, and
post_deploy dies if the rendered set comes back empty — /etc is never
converged against an unknown contract.

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
when absent. Undeclared template-name collisions grade the same way. Locally
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
contracts, proxy CIDRs, log metadata, and config hashes. Default `auto` keeps
legacy disabled nodes informational.

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
