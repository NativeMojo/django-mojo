# Node deployment tooling (`mojo.deploy`)

Two standalone programs that ship inside the django-mojo wheel and run on the
node itself, outside Django:

| Module | What it does |
|---|---|
| `mojo.deploy.config_sync` | Pulls this node's `django.conf` from S3 and installs it atomically at 0600 |
| `mojo.deploy.check_setup` | Read-only audit of an AWS account for security and durability gaps |

Both are invoked with `python3 -m`:

```bash
python3 -m mojo.deploy.config_sync --dry-run
python3 -m mojo.deploy.check_setup --section s3,iam
```

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

`check_s3` still reports a missing `AWS_CERT_BUCKET`. That finding belongs to
the retired `certbot_sync.py` lineage-sharing flow and should be removed with
it.

---

## What stays in `django-mojo-skeleton`

`ec2_bootstrap.sh`, `ec2_deploy.sh` and `post_deploy.sh` stay. They are the
per-project deployment shape — which repo, which branch, which nginx config,
which systemd units — not framework behaviour, and a project is expected to
edit them. Only code that must be identical everywhere moves into the package.
