# Fleet code deploy

A verified GitHub webhook records the requested commit and starts the existing
fleet fan-out. One runner performs the locked migration first; after that node
returns successfully, the same commit and framework version are sent to the
remaining runners.

## The deliberately small node contract

The parent runs the configured `EDGE_DEPLOY_SCRIPT` with:

```text
--sha <commit> --framework <version> --deployment <uuid> [--migrate]
```

The configured path must exist and be executable. SHA and version values are
validated before they enter argv. The parent does not read the script, assign a
contract version, inspect its source, or reject it for deployment policy.

The project-owned script returns zero only after:

- the real `nginx -t` accepted the installed configuration; and
- the restarted candidate API returned exactly HTTP 200.

Redirects do not count as API health. A candidate that cannot import Django
fails during migration or static collection and is rolled back entirely by
shell code.

## Status ownership

Current parents record node evidence and the canary terminal status after the
script returns. The script no longer stops its calling engine or depends on
`manage.py deploy_status` for normal operation. After recording the result,
the parent detaches a short engine recycle so the completed job can be
acknowledged before the old process exits.

One predecessor-generation callback remains solely for adoption: when the
parent does not set `MOJO_DEPLOY_PARENT_STATUS`, a healthy candidate reports
the legacy canary status once and schedules its own recycle. It never runs
before the candidate passes nginx and HTTP checks.

## Failure handling

An unconfigured path, missing execute bit, exec error, timeout, or non-zero
script exit is reported as node failure. A timeout terminates the whole update
process group, waits briefly, and then kills and reaps the group if necessary.
This prevents a timed-out pip, migration, or rollback child from continuing
after the parent has declared failure.

Redis remains short-lived coordination and `PlatformDeployment` remains the
durable attempt record. Neither adds another release gate. Independent MojoSec
observation is outside this path and cannot block deployment.
