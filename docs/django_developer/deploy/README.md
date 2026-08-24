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

## What a deployment does

`update.sh` accepts:

```text
--sha <commit> --framework <version> --deployment <uuid> [--migrate]
--manual
```

It takes the project lock, records the previous commit and installed framework
version without importing Django, fetches the named commit, checks it out, and
hands the transaction to that commit's `aws/post_deploy.sh`.

`post_deploy.sh` then:

1. records the previous host-file bytes and release identity;
2. installs project requirements and the requested django-mojo version;
3. runs the locked migration on the canary, then `collectstatic`;
4. renders and copies the project's nginx, cron, and systemd files;
5. runs the real `nginx -t`;
6. restarts `mojo-asgi`, reloads nginx, and requires the probe URL to return
   exactly HTTP 200.

Those last two checks are the only release gates. There is no TLS semantic
parser, certificate-lineage preservation, node-role authority, file-integrity
gate, trusted-change journal, ownership policy, or request-service policy in
the deployment path. Independent observability tools may report drift, but
they cannot veto a release.

## Rollback and interrupted runs

Once `post_deploy.sh` starts, it is the single rollback owner. It writes a
mechanical transaction under `var/deploy-rollback/active`, and traps ordinary
errors plus TERM, INT, and HUP. A rollback restores:

- the previous git commit;
- the previous exact django-mojo version and that commit's requirements;
- the nginx and mojo-asgi files that existed before the attempt;
- the prior service configuration, followed by `nginx -t`, restart, reload,
  and an exact-200 probe of the restored API.

The transaction is removed only after a healthy candidate or healthy rollback.
If SIGKILL or a machine loss interrupts it, the next `update.sh` detects the
marker and recovers it before recording a new previous release.

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
