# Fleet code deploy

A verified GitHub webhook records the requested commit and starts the existing
fleet fan-out. Each attempt freezes two live runner cohorts:

- `edge` is the API cohort;
- `platform-deploy` is the specialized/code cohort.

Their union, capped at 128 unique runners, is the deployment roster. A runner
advertising both channels is classified as API. A deployment with no live API
runner fails before framework resolution, migration, or node mutation because
there is no safe migration canary.

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

Projects may override the complete argv, commonly with
`["sudo", "-n", "/opt/api/aws/update.sh"]`. An existing `aws/update.sh` that
delegates to `python3 -m mojo.deploy locate update.sh` remains current
automatically and requires no project source update. SHA, framework version,
deployment UUID and node type are validated before mutation. The parent does
not inspect script source or add deployment security-policy gates.

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
or package mutation. The unit owns the 30-minute transaction timeout and
15-minute rollback window, so restarting the job engine cannot orphan an
update. The parent process waits beyond both windows instead of killing a
legitimate rollback.

Current parents record node evidence after the script returns. API nodes then
detach a short engine recycle so the completed job can be acknowledged before
the old process exits. `code` nodes receive no generic restart. A custom
profile owns its service restart; if that restart kills the caller, the
replacement engine consumes the transaction's bounded outcome and exact local
identity to finalize the same deployment UUID.

One predecessor-generation callback remains solely for API adoption: when the
parent does not set `MOJO_DEPLOY_PARENT_STATUS`, the healthy migrating canary
reports legacy status once. It never runs before nginx and HTTP checks, and it
never runs on non-API nodes.

## Failure handling

An invalid type/cohort, missing custom profile, exec error, timeout, or non-zero
script exit is reported as node failure. Mechanical state under
`/var/lib/django-mojo-deploy/active` restores the previous checkout, declared
dependencies, exact framework and typed lifecycle. An interrupted transaction
is recovered before the next candidate starts.

Redis remains short-lived coordination and `PlatformDeployment` remains the
durable attempt record. Both the installed identity and node evidence include
the node type. Neither adds another release gate; independent MojoSec
observation remains outside the deployment path.
