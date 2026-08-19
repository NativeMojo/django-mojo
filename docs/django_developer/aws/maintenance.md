# Managed-service maintenance

Applying the upgrades [version drift](version_drift.md) finds, from the Admin
portal, with a record of who asked for what.

The scanner answers *what is out of date*. This answers *and now do one of
them* — without an operator dropping into the AWS console, where the action
leaves no trace in this installation at all.

## Layers

| Layer | File | Job |
|---|---|---|
| Helpers | `mojo/helpers/aws/rds.py`, `mojo/helpers/aws/elasticache.py` | Engine-version reads and the four modify operations |
| Service | `mojo/apps/aws/services/maintenance.py` | Report + cache, target re-derivation, single flight, dispatch, poll |
| REST | `mojo/apps/aws/rest/maintenance.py` | Permission AND, typed confirmation, wire-safe errors |
| Portal | `assets/features/platform/maintenance.js` | The Maintenance route |

## Helpers

Module-level functions, not classes — they hold no state beyond the `client=`
seam each one accepts. Clients come from `mojo.helpers.aws.client.get_client`
with `max_attempts=1`; settings come from `settings.get_static`, mirroring
`version_drift.py`.

```python
from mojo.helpers.aws import rds, elasticache

rds.instance_statuses()                  # {id: {status, engine_version, pending_version}}
rds.cluster_statuses()
rds.instance_status("mojo-prod-postgres")   # filtered describe — the poll path
rds.cluster_status("mojo-prod-aurora")
rds.modify_instance_engine_version(identifier, target, apply_immediately,
                                   parameter_group=None, client=None, region=None)
rds.modify_cluster_engine_version(...)

elasticache.group_statuses()             # {id: {is_replication_group, members, status, ...}}
elasticache.group_status("mojo-prod-redis")
elasticache.resolve_kind("mojo-prod-redis")  # "replication_group" | "standalone"
elasticache.modify_replication_group_engine_version(...)
elasticache.modify_cache_cluster_engine_version(...)
```

Three things about these that are easy to get wrong:

- **`mutation` is passed explicitly, never inferred.** Every call goes through
  `ProviderCaller.call(operation, callback, iam_action=..., mutation=...)`.
  `ProviderClient` derives the flag from a method-name prefix and `modify_` is
  not one of them, so routing these through it would record an upgrade as a
  read — and a failed upgrade would report `mutation_state="none"`, which is
  the single most misleading thing it could say.
- **`AllowMajorVersionUpgrade` is RDS-only.** `ModifyReplicationGroup` and
  `ModifyCacheCluster` do not define that member; sending it is an API error.
  The tests stub these against the real service model for exactly this reason —
  a plain `Mock` accepts any keyword and would have hidden it.
- **`group_status` reads the whole `DescribeCacheClusters` page and narrows in
  process.** There is no replication-group filter on that API, and the
  `ReplicationGroup` shape carries no engine version, so the only provider-side
  alternative is one describe per member — an N+1 on a rate-limited API for a
  poll that repeats every ten seconds.

A cache group's status is that of its **least settled member**. A group whose
replica is still rebooting is not available, whatever the primary says.

## Service

```python
from mojo.apps.aws.services import maintenance

maintenance.report(refresh=False)          # drift scan + live status, cached
maintenance.offered_target(kind, resource_id, report)
maintenance.apply_upgrade(actor, kind, resource_id, target_version, apply_immediately)
maintenance.resource_status(kind, resource_id, target)
```

`kind` is one of `rds-instance`, `rds-cluster`, `elasticache` — the same values
the drift scanner puts on its findings.

### The report

`VersionDriftScanner().scan()` (untouched, still read-only) plus live status
per finding, cached in `django.core.cache` under
`mojo:aws:maintenance:<region>` for 600 seconds. `refresh=True` bypasses it.
A cache outage costs a scan, never the page.

The envelope adds three things to the scan:

| Key | Meaning |
|---|---|
| `status` / `pending_version` / `settled` | Live state per finding; `pending_version` is the version AWS accepted but has not applied |
| `warnings` | The scan's warnings plus any denied status describe, each naming its IAM action |
| `scheduled` | Whether `AWS_VERSION_DRIFT_ENABLED` is on — an empty page means something different when nothing is scanning |

### Applying

`apply_upgrade` will not apply a version it was handed. It re-derives the
target through `offered_target` against the server's own report; a
`target_version` that does not match is a 409 `upgrade_not_offered`. A stale
tab and a hand-made request are the same case, and both are refused.

Then a single-flight key, `mojo:aws:maintenance:apply:<kind>:<id>`, held for
120 seconds via `cache.add`. When `add` returns False the key is **read back**,
because "somebody holds it" and "the backend did nothing" need different
answers:

| Situation | Answer |
|---|---|
| Key readable, holder present | 409 `upgrade_in_progress` |
| Key unreadable, or `add` itself raised | 503 `cache_unavailable` |

A cache that cannot answer is never a go-ahead — the thing being guarded is a
second concurrent major-version upgrade against a live database.

The key is **not released** when the provider call fails. A failed mutation is
`attempted` or `unknown`, never proof that nothing happened, so a retry racing
an in-flight change is worse than waiting out the 120 seconds.

ElastiCache dispatch calls `resolve_kind` **at apply time**, not from the
report: the report can be ten minutes old, and picking the wrong modify
operation is a failed mutation against a live cache.

### Success is a version change

`resource_status` returns both `settled` and `upgraded`, and they are not the
same claim:

- `settled` — every member is `available`. AWS finished doing something.
- `upgraded` — **every** member's `engine_version` equals the target.

A resource that returns to `available` still on its old engine has not been
upgraded, and the portal says so rather than reporting success.

## Errors

Every failure leaves the service as a `MaintenanceError` carrying
`ProviderCallError.detail()` — never raw botocore text, which can contain
credentials, signed URLs, and request parameters.

| `error_code` | Status | Cause |
|---|---|---|
| `invalid_request` | 400 | Unknown kind, or a missing identifier |
| `upgrade_not_offered` | 409 | The server is not offering that version for that resource |
| `upgrade_in_progress` | 409 | Another apply holds the single-flight key |
| `cache_unavailable` | 503 | The coordination cache could not confirm the key |
| `provider_denied` | 403 | IAM refused; `data.failure.iam_action` names what to grant |
| `provider_unavailable` | 503 | Retryable provider failure |
| `provider_error` | 502 | Anything else from the provider |
| `infrastructure_external` | 403 | `INFRASTRUCTURE_MODE` is `external` — see below |

## Infrastructure mode

Before anything else, the apply asks whether this installation's infrastructure
is the portal's to change at all. `INFRASTRUCTURE_MODE = "external"` says it is
not, and the apply answers 403 `infrastructure_external`.

That check is the **first statement in the endpoint body** — ahead of
`_require_manage_tier`, ahead of body parsing. It is a property of the
installation, not of the caller, so a caller who is also missing the platform
tier must be told about the mode rather than about their grants: no additional
grant would change the answer.

`apply_upgrade` carries the same refusal as a service-layer backstop, raising
`MaintenanceError(..., "infrastructure_external", 403)` before it scans or
dispatches. That path exists for non-REST callers only; reaching it means the
REST gate was bypassed.

The framework leg is gated identically. Full contract, including what is
deliberately **not** gated and the `EDGE_FRAMEWORK_VERSION` instruction for
external installations: [Infrastructure mode](infrastructure_mode.md).

## Permissions

Reads need `manage_aws`. The apply needs `manage_aws` **and** a platform
management tier — superuser, `manage_platform`, or `admin`.

That is an AND, and it cannot be expressed by the decorator: every permission
`requires_global_perms` lists is satisfied on its own. So the endpoint carries
`@md.requires_global_perms("manage_aws")` plus an in-body
`_require_manage_tier(request)`. `manage_aws` alone is the grant that reads
CloudWatch charts; it is not the grant that reboots the production database.

The apply also carries `@md.denies_key_backed_session()` and
`@md.requires_fresh_auth(seconds=600)`, matching the platform deploy endpoints.

## Required IAM

Reads (already needed by version drift):

```
rds:DescribeDBInstances, rds:DescribeDBClusters,
rds:DescribeDBEngineVersions, rds:DescribeDBMajorEngineVersions,
elasticache:DescribeCacheClusters, elasticache:DescribeCacheEngineVersions,
elasticache:DescribeReplicationGroups
```

Writes (new — without these the page reads fine and every apply is a 403 that
names the missing action):

```
rds:ModifyDBInstance
rds:ModifyDBCluster
elasticache:ModifyReplicationGroup
elasticache:ModifyCacheCluster
```

`elasticache:DescribeReplicationGroups` is also on the apply path: it is what
decides which of the two ElastiCache modify operations to call.

## The framework leg

The same page also updates django-mojo itself, through
`mojo/apps/account/services/admin_platform.py`:

- `framework_overview(request)` — entirely derived from
  `framework_version.status()`. There is one PyPI check in this codebase and
  this is not a second one. `blocked_reason` is `update_unavailable`,
  `requires_superuser` (a pin is set and the caller is not a literal
  superuser), `no_converged_deployment`, or `infrastructure_external` — which
  overrides the other three, because the endpoint would refuse whatever the
  version facts say. The version facts themselves stay truthful.
- `apply_framework_update(request, version)` — **clears** any pin via
  `system_settings.set_value(user, FRAMEWORK_VERSION_KEY, "")`, then
  `platform_deploy.same_sha_retry(last_converged_deployment())`. Refuses first
  when `INFRASTRUCTURE_MODE` is `external` (the service-layer backstop; the
  endpoint has already answered HTTP for ordinary callers).

Writing the requested version *into* the pin would look like it worked and
would silently freeze the fleet at today's release — invisible until the next
one. The framework version resolves at install time, so redeploying the last
converged commit is what actually installs the new release.

`platform_deploy.last_converged_deployment()` is the row-returning sibling of
`last_converged_framework()`: converged, not merely released, because that
status is the reconciler's proof the commit actually runs on this fleet.

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Region for every describe and modify, and the report cache key |
| `AWS_VERSION_DRIFT_ENABLED` | `False` | Reported as `scheduled` so an empty page can distinguish "nothing pending" from "nothing scanning" |
| `AWS_VERSION_DRIFT_DEADLINE_DAYS` | `180` | Consumed by the scanner; see [version_drift.md](version_drift.md) |

## Preview

`bin/admin_preview` renders the whole page without an AWS account:

```
bin/admin_preview --maintenance-state findings
```

States: `findings`, `denied`, `in_flight`, `stalled`, `unavailable`,
`framework_pinned`, `framework_none`, `clear`. `stalled` is the one that
exercises the settled-but-not-upgraded copy.
