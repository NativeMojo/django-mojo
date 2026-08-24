# Project deployment scripts

Every project owns two small executable files:

```
aws/update.sh
aws/post_deploy.sh
```

Create or refresh them explicitly:

```bash
python3 -m mojo.deploy export-scripts --dest aws --force
git add aws/update.sh aws/post_deploy.sh
git update-index --chmod=+x aws/update.sh aws/post_deploy.sh
```

These are copies, not shims. They never locate or execute deployment code from
the currently installed django-mojo package. This is the recovery boundary: a
release that cannot import Django can still be replaced by the next checkout.
`--force` replaces both destination files, so review and commit the exported
bytes instead of keeping local deployment changes only on a node.

## What a deployment does

`update.sh` accepts:

```text
--sha <commit> --framework <version> --deployment <uuid> [--migrate]
--manual
```

It takes the project lock, records the previous commit and installed framework
version without importing Django, fetches the named commit, checks it out, and
hands the transaction to that commit's `aws/post_deploy.sh`.

`--manual` is the operator path: it deploys `origin/main`, upgrades to the
newest available django-mojo, and does not run migrations. It refuses deploy
arguments in the same invocation.

`post_deploy.sh` then:

1. records the previous host-file bytes and release identity;
2. installs project requirements and the requested django-mojo version;
3. runs the locked migration on the canary, then `collectstatic`;
4. renders and copies the project's nginx, cron, and systemd files;
5. runs the real `nginx -t`;
6. restarts `mojo-asgi`, reloads nginx, and requires the probe URL to return
   exactly HTTP 200.

Those last two checks are the final acceptance gates. A failed required command
earlier in the transaction, such as dependency installation, migration, static
collection, rendering, or service restart, also rolls the candidate back.
There is no TLS semantic parser, certificate-lineage preservation, node-role
authority, file-integrity gate, trusted-change journal, ownership policy, or
request-service policy in the deployment path. Independent observability tools
may report drift, but they cannot veto a release.

## Project inputs

| Variable | Default | Meaning |
|---|---|---|
| `PROJ_PATH` | `/opt/api` | Project checkout |
| `PROBE_URL` | `https://127.0.0.1/api/version` | Candidate and rollback URL; it must answer exactly HTTP 200 |
| `APP_USER` | `ec2-user` | Value substituted into rendered cron/systemd templates |
| `WEB_USER` | `www` | Value substituted into rendered service templates |
| `ASGI_WORKERS` | `4` | Worker count substituted into rendered service templates |

`NGINX_ETC`, `SYSTEMD_ETC`, and `CRON_ETC` default to their normal `/etc`
locations and exist as test seams. Production projects should not redirect
them.

## Rollback and interrupted runs

Once `post_deploy.sh` starts, it is the single rollback owner. It writes a
mechanical transaction under root-owned
`/var/lib/django-mojo-deploy/active`, and traps ordinary errors plus TERM,
INT, and HUP. A rollback restores:

- the previous git commit;
- the previous exact django-mojo version and that commit's requirements;
- the nginx, cron, and systemd files that existed before the attempt;
- the restored systemd configuration, followed by `nginx -t`, a mojo-asgi
  restart, nginx reload, and an exact-200 probe of the restored API.

The transaction is removed only after a healthy candidate or healthy rollback.
If SIGKILL or a machine loss interrupts it, the next `update.sh` asks the
root-owned transaction to recover before recording a new previous release.

## Compatibility with the predecessor launcher

The first deploy that adopts these files can still be started by the previous
django-mojo job engine. New deploy parents set `MOJO_DEPLOY_PARENT_STATUS=1`
and record success after the script returns. A predecessor does not set it, so
the verified candidate performs one legacy `deploy_status` callback and
detaches an engine recycle. This bridge runs only after nginx and the API have
passed; a broken candidate never needs Django to initiate rollback.

## Pure rendering

`python3 -m mojo.deploy render` only writes files beneath `--dest`. It does
placeholder substitution and project overlay handling; it does not inspect or
mutate live nginx, invoke an integrity sensor, or enforce a node role.

## Node diagnostics

`python3 -m mojo.deploy.check_node --section shims` reports whether the two
project scripts exist, are executable, pass `bash -n`, or still contain the
obsolete `mojo.deploy locate` pattern. These findings are advisory. They are
not deployment gates.

## Other pre-Django node tools

Only the two shell deployment scripts moved into the project. The remaining
`python3 -m mojo.deploy.*` utilities are still framework-owned and run without
loading project Django settings.

### `config_sync`

`python3 -m mojo.deploy.config_sync` pulls the node's `django.conf` from S3,
validates any configured digest and typed Admin override, and installs the
result atomically at mode 0600. S3 is authoritative; the node never writes the
base configuration back.

### Admin fleet overrides

An optional `django.override.json` object may set only keys present in
`CONFIG_SYNC_OVERRIDE_ALLOWED_KEYS` and the framework's typed override
registry. Its digest is required. Invalid or concurrent updates leave the last
working `django.conf` untouched. The Admin publisher is separately bounded by
`ADMIN_FLEET_CONFIG_ALLOWED_KEYS`; the effective writable set is the
intersection of both allowlists and framework support.

### `check_setup`

`python3 -m mojo.deploy.check_setup` is a read-only AWS account audit. It runs
without Django and reports findings without modifying account resources. It is
distinct from the Django-aware `aws-check` convergence tool.

### `certbot_sync`

`python3 -m mojo.deploy.certbot_sync` shares one Let's Encrypt lineage through
S3. The elected primary renews and publishes the complete lineage; replicas
verify matching certificate/key material before an atomic local install and
nginx reload.

### `check_node`

`python3 -m mojo.deploy.check_node` audits one node. Its project-script checks
are advisory diagnostics, including under `--require-shims`; deployment itself
does not call this tool.

### `jobman`

`python3 -m mojo.deploy.jobman` starts, stops, and reports the foreground job
engine and scheduler. Cron remains the normal start backstop. A successful
deployment schedules an engine recycle only after the invoking job has
returned and recorded its result.

### `node_setup`

`python3 -m mojo.deploy.node_setup` converges the framework's systemd, cron,
and project-directory setup outside Django. It is a provisioning/operator tool,
not a release acceptance gate.

### MojoSec

`python3 -m mojo.deploy.mojosec` operates the independent observe-only sensor.
Its health and drift findings are deliberately outside the update transaction:
they can alert operators but cannot refuse or roll back an application deploy.
