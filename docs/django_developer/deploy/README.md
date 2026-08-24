# Project deployment scripts

django-mojo owns the deployment transaction. The normal entry point is the
packaged `update.sh`, resolved without importing project Django:

```bash
python3 -m mojo.deploy locate update.sh
```

`EDGE_DEPLOY_SCRIPT` defaults to a small `sudo` command that resolves and runs
that entry point. A project may keep the same permanent endpoint as an
executable `aws/update.sh` shim, and may keep an `aws/post_deploy.sh` shim for
project environment variables. Existing API projects whose shims call
`mojo.deploy locate` require no source update: each release automatically uses
the installed framework scripts. When a legacy shim is launched as the
application account, the packaged entry point re-enters itself once through
passwordless `sudo` before it creates the transient unit or changes anything.

## The shim contract

A shim contains project setup only, then delegates:

```bash
#!/bin/bash
export PROBE_URL="https://127.0.0.1/api/version"  # optional project delta
target="$(python3 -m mojo.deploy locate update.sh)" || exit 1
exec bash "$target" "$@"
```

The post-deploy equivalent locates `post_deploy.sh`. `update.sh` installs the
requested candidate framework *before* resolving the post-deploy body, so a
candidate that repairs activation code supplies its own post-deploy logic. None
of these helpers imports project Django; a release that cannot load Django can
therefore still be replaced and rolled back.

`python3 -m mojo.deploy export-scripts --dest aws --force` remains available
for debugging or an intentional full-script override. It copies the current
framework bodies; ordinary projects should keep the small locator shim instead
of vendoring deployment mechanics. A project that intentionally keeps a full
`aws/post_deploy.sh` copy owns that override and must keep its action arguments
current; replace an obsolete copy with the locator shim or remove it.

## What a deployment does

`update.sh` accepts:

```text
--sha <commit> --framework <version> --deployment <uuid>
    [--node-type <type>] [--migrate]
--manual [--node-type <type>]
```

`api` is the default node type. Before any checkout or package mutation, the
script moves the complete transaction into a bounded transient systemd unit.
It then takes the project lock, recovers any interrupted transaction, records
the previous commit/framework/type without importing Django, fetches the named
commit, checks it out as `APP_USER`, installs its declared dependencies and
the requested framework, and invokes the candidate activation body.

`--manual` is the operator path: it deploys `origin/main`, upgrades to the
newest available django-mojo, and does not run migrations. It refuses deploy
arguments in the same invocation. `--migrate` is valid only for `api`.

Dependency selection is deliberately small and deterministic:

- use `aws/deploy/requirements.txt` when it exists;
- otherwise use the root `requirements.txt`;
- otherwise install no project manifest.

Only one manifest is installed. Rollback retains a copy of the previous
release's selected manifest and reinstalls it with the previous exact
django-mojo version.

## Typed activation

Every node runs the same checkout/dependency/framework transaction, followed
by exactly one activation path:

| `EDGE_DEPLOY_NODE_TYPE` | Activation |
|---|---|
| `api` (default) | Built-in Django, nginx, systemd and exact-HTTP-200 lifecycle |
| `code` | Checkout and dependencies only; no migration, Django command, nginx, systemd or HTTP probe |
| any other valid name | Project profile `aws/deploy/<type>.sh` |

The built-in `api` path:

1. runs `manage.py check` before changing host configuration;
2. runs `migrate_locked --noinput` only on the API canary when `--migrate` is
   present, then runs `collectstatic`;
3. renders and installs the project's nginx, cron, and systemd files;
4. applies explicit removals from `aws/node_retired.conf`;
5. runs the real `nginx -t`;
6. restarts `mojo-asgi`, reloads nginx, and requires `PROBE_URL` to answer
   exactly HTTP 200.

Those last two checks are the final acceptance gates. A failed required command
earlier in the transaction, such as dependency installation, migration, static
collection, rendering, or service restart, also rolls the candidate back.
There is no TLS semantic parser, certificate-lineage preservation, node-role
authority, RPM verification, file-integrity gate, trusted-change journal,
ownership policy, or request-service policy in the deployment path.
Independent observability tools may report drift, but they cannot veto a
release.

### Custom profiles

A custom type is for a node such as a Maestro Sites worker whose service and
health check are not the API's. The profile must already exist in the serving
checkout before `EDGE_DEPLOY_NODE_TYPE` is changed; this staged adoption keeps
the first typed deploy rollbackable. The candidate profile must parse as shell
and accept three verbs:

```text
aws/deploy/sites.sh preflight
aws/deploy/sites.sh restart
aws/deploy/sites.sh probe
```

`preflight` must make no host mutation. `restart` activates the already
installed checkout and may restart the job engine that launched the deploy.
`probe` returns zero only when that node's candidate is usable. The framework
does not run Django, nginx, systemd or curl on behalf of a custom profile.

Profiles receive this fixed environment:

| Variable | Meaning |
|---|---|
| `PROJ_PATH` | Project checkout |
| `MOJO_DEPLOY_CANDIDATE_SHA` / `MOJO_DEPLOY_PREVIOUS_SHA` | Candidate and previous commits |
| `MOJO_DEPLOY_CANDIDATE_FRAMEWORK` / `MOJO_DEPLOY_PREVIOUS_FRAMEWORK` | Candidate and previous framework versions |
| `MOJO_DEPLOY_NODE_TYPE` | Selected custom type |
| `MOJO_DEPLOY_DEPLOYMENT` | Deployment UUID |
| `MOJO_DEPLOY_STARTED_AT` | Transaction start, Unix seconds |
| `MOJO_DEPLOY_ROLLBACK` | `0` during activation; `1` during recovery |

On recovery, the candidate profile receives `restart` with
`MOJO_DEPLOY_ROLLBACK=1`, then the saved previous profile receives `restart`
and `probe` with the same flag. A profile should make those operations
idempotent.

## Project inputs

| Variable | Default | Meaning |
|---|---|---|
| `PROJ_PATH` | `/opt/api` | Project checkout |
| `PROBE_URL` | `https://127.0.0.1/api/version` | API candidate and rollback URL; it must answer exactly HTTP 200 |
| `APP_USER` | `ec2-user` | Account that owns Git operations; also substituted into rendered cron/systemd templates |
| `WEB_USER` | `www` | Value substituted into rendered service templates |
| `ASGI_WORKERS` | `4` | Worker count substituted into rendered service templates |

`NGINX_ETC`, `SYSTEMD_ETC`, and `CRON_ETC` default to their normal `/etc`
locations and exist as test seams. Production projects should not redirect
them.

## Rollback and interrupted runs

The complete update runs as a transient oneshot unit with a 30-minute
activation limit. If systemd stops a timed-out transaction, it allows up to a
further 15 minutes for the TERM-triggered rollback before forcing the unit
down. This boundary is established before mutation, so a custom profile may
restart its own job engine without killing the transaction.

The shell writes a mechanical transaction under root-owned
`/var/lib/django-mojo-deploy/active`, and traps ordinary errors plus TERM, INT,
and HUP. An API rollback restores:

- the previous git commit;
- the previous exact django-mojo version and that commit's requirements;
- the nginx, cron, and systemd files that existed before the attempt;
- the restored systemd configuration, followed by `nginx -t`, a mojo-asgi
  restart, nginx reload, and an exact-200 probe of the restored API.

Custom rollback uses the profile protocol above; `code` restores the checkout
and packages and leaves activation to its external supervisor. The transaction
is removed only after a healthy candidate or healthy rollback. If SIGKILL or a
machine loss interrupts it, the next `update.sh` recovers the root-owned
transaction before recording a new previous release.

Python packages are installed into the existing environment. Rollback
reinstalls the previous declared requirements and exact framework version, but
does not promise to uninstall every transitive package introduced only by the
failed candidate. Projects that require byte-for-byte dependency rollback need
an external immutable environment strategy.

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

`python3 -m mojo.deploy.check_node --node-type <type>` makes diagnostics match
the node lifecycle. Non-API types skip API-only cron, systemd, nginx,
certificate, config-plane and legacy checks; a custom type also verifies that
`aws/deploy/<type>.sh` exists and parses. The jobs check expects `edge` on API
nodes and `platform-deploy` on non-API nodes.

`--section shims` reports whether optional project scripts exist, are
executable, and pass `bash -n`. A `mojo.deploy locate` shim is the valid
permanent endpoint. These findings are advisory and are not deployment gates.

## Other pre-Django node tools

The shell deployment bodies and the remaining `python3 -m mojo.deploy.*`
utilities are framework-owned and run without loading project Django settings.

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

`python3 -m mojo.deploy.check_node` audits one node. Pass the same type as the
node's static setting, for example `--node-type sites`. Its project-script and
profile checks are advisory diagnostics, including under `--require-shims`;
deployment itself does not call this tool.

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
