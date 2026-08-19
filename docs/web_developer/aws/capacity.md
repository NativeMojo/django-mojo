# Capacity actions

Add or remove an app node, an RDS reader, or an ElastiCache replica.

```
GET  /api/aws/capacity          the fleet, reader and replica picture
GET  /api/aws/capacity/status   one capacity operation's progress
POST /api/aws/capacity/apply    request ONE capacity change
```

Every apply is asynchronous: it returns an **operation** immediately and the
work is proven by polling `/status`. Nothing here reports success on "AWS
accepted it".

## Permissions

| Endpoint | Needs |
|---|---|
| `GET /api/aws/capacity` | `manage_aws` |
| `GET /api/aws/capacity/status` | `manage_aws` |
| `POST /api/aws/capacity/apply` | `manage_aws` **AND** a literal superuser, an interactive (non-key) session, and fresh auth within 600s |

The apply gate is stricter than the Maintenance apply next door. An engine
upgrade changes a resource the installation already owns; these actions create
and destroy infrastructure that serves production traffic and bills by the
hour. `manage_platform` is **not** sufficient.

A key-backed session gets 403. A session whose last interactive authentication
is older than 600 seconds gets **440 `reauth_required`** — re-authenticate and
retry.

## `GET /api/aws/capacity`

`?refresh=1` bypasses the 2-minute server cache.

```json
{
  "status": true,
  "data": {
    "schema_version": 1,
    "region": "us-east-1",
    "mode": "managed",
    "generated_at": "2026-08-18T18:00:00+00:00",
    "node_id_pinned": false,
    "nodes": {
      "balancers": [{"name": "mojo-api-nlb", "arn": "...", "type": "network", "state": "active"}],
      "groups": [{"arn": "...", "name": "mojo-api", "target_type": "instance", "port": 443, "protocol": "TCP"}],
      "instances": [
        {"id": "i-0a1b…", "name": "mojo-api-a", "state": "healthy",
         "instance_state": "running", "instance_type": "m6i.large",
         "zone": "us-east-1a", "healthy": true, "self": true, "primary": true,
         "added_by_capacity": false, "groups": ["arn:…targetgroup/mojo-api/…"]}
      ],
      "self": "i-0a1b…",
      "self_check": "matched"
    },
    "databases": [
      {"identifier": "mojo-prod-aurora", "kind": "aurora", "engine": "aurora-postgresql",
       "status": "available", "writer": "mojo-prod-aurora-1",
       "readers": ["mojo-prod-aurora-2"],
       "reader_endpoint": "…cluster-ro-….rds.amazonaws.com",
       "endpoint": "…cluster-….rds.amazonaws.com"}
    ],
    "caches": [
      {"identifier": "mojo-prod-redis", "status": "available", "replica_count": 1,
       "cluster_enabled": false, "automatic_failover_on": true, "multi_az_on": true,
       "members": [{"id": "…-001", "role": "primary"}, {"id": "…-002", "role": "replica"}],
       "min_replicas": 1, "blocked_reason": null}
    ],
    "warnings": [
      {"code": "databases", "iam_action": "rds:DescribeDBClusters",
       "aws_code": "AccessDenied",
       "message": "rds:DescribeDBClusters did not answer, so this section could not be read"}
    ],
    "actions": {
      "add_node":           {"offered": true,  "blocked_reason": null},
      "drain_node":         {"offered": false, "blocked_reason": "last_healthy_target"},
      "terminate_node":     {"offered": true,  "blocked_reason": null},
      "add_reader":         {"offered": true,  "blocked_reason": null},
      "remove_reader":      {"offered": false, "blocked_reason": "no_reader"},
      "set_cache_replicas": {"offered": true,  "blocked_reason": null}
    }
  }
}
```

Three things a client must not re-derive:

- **`actions`.** The server computes what it would accept. Render a control
  only where `offered` is true, and show `blocked_reason` where it is not. A
  client that decides for itself will eventually offer a button the gate
  refuses.
- **`self_check`.** `"matched"` means the server identified which instance is
  serving the request. `"unavailable"` means it could not — that is **not** a
  passed check, and a UI must say so before letting the operator remove a node.
- **`mode`.** `"external"` means this installation's AWS estate is applied by
  an external IaC pipeline. Every apply answers 403; the read still works.

`warnings` carries a degraded section. A section that could not be read comes
back empty **and** named here — never silently empty.

## `POST /api/aws/capacity/apply`

Common fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `action` | string | yes | One of `add_node`, `drain_node`, `terminate_node`, `add_reader`, `remove_reader`, `set_cache_replicas` |
| `resource` | string | yes, except `add_node` | Instance id, database identifier, or replication-group id |
| `confirm_resource` | string | yes | Must equal `resource` EXACTLY. For `add_node` (which has no resource) it must be the literal `"add_node"` |

Per-action fields:

| Action | Extra fields |
|---|---|
| `set_cache_replicas` | `count` (integer, **no default**), `apply_immediately` (boolean, **no default**, must be `true`) |

`confirm_resource` is checked **before any provider call**. `count` and
`apply_immediately` have no defaults: a missing field is a question that was
never asked, not a "no".

### Bodies

```json
{"action": "add_node", "confirm_resource": "add_node"}

{"action": "drain_node", "resource": "i-0a1b…", "confirm_resource": "i-0a1b…"}

{"action": "terminate_node", "resource": "i-0a1b…", "confirm_resource": "i-0a1b…"}

{"action": "add_reader", "resource": "mojo-prod-aurora",
 "confirm_resource": "mojo-prod-aurora"}

{"action": "remove_reader", "resource": "mojo-prod-aurora-2",
 "confirm_resource": "mojo-prod-aurora-2"}

{"action": "set_cache_replicas", "resource": "mojo-prod-redis",
 "confirm_resource": "mojo-prod-redis", "count": 2, "apply_immediately": true}
```

### Response

```json
{
  "status": true,
  "data": {
    "schema_version": 1,
    "id": "9f1c…",
    "action": "add_node",
    "resource": "i-0a1b…",
    "state": "running",
    "phase": "capturing",
    "phases": ["capturing", "launching", "booting", "converging",
               "proving", "registering", "settling"],
    "message": "requested",
    "error_code": null,
    "warnings": [],
    "detail": {"source_instance": "i-0a1b…", "source_name": "mojo-api-a"}
  }
}
```

`id` is what you poll.

## `GET /api/aws/capacity/status?operation=<id>`

Returns the same shape, updated. Poll roughly every 10 seconds until `state` is
no longer `running`.

| `state` | Meaning |
|---|---|
| `running` | Still working. `phase` says which step; `phases` is the whole ladder, so `phases.indexOf(phase)` gives "step N of M" |
| `done` | Reached a **proven** steady state. `message` is the sentence to show |
| `failed` | Stopped. `error_code` says why; `message` says what state the world is in |

`warnings` may be non-empty on a `done` operation. They are things that did not
work but did not invalidate the result — show them.

**404 `operation_not_found`** for an unknown id: an operation record lives 90
minutes.

### Phase ladders

| Action | Phases |
|---|---|
| `add_node` | `capturing` → `launching` → `booting` → `converging` → `proving` → `registering` → `settling` |
| `drain_node` | `draining` |
| `terminate_node` | `terminating` |
| `add_reader` | `creating` → `settling` |
| `remove_reader` | `deleting` |
| `set_cache_replicas` | `scaling` → `settling` |

Terminal operations report `phase: "complete"`.

## Error codes

Refusals return HTTP `4xx`/`5xx` with
`{"status": false, "error": "...", "error_code": "...", "data": {...}}`.
Failures **after** the apply was accepted appear as `error_code` on the
operation record instead.

### From the apply

| `error_code` | HTTP | Meaning |
|---|---|---|
| `infrastructure_external` | 403 | `INFRASTRUCTURE_MODE=external`. No caller may change capacity here. `data` carries `mode` and `setting` |
| `invalid_request` | 400 | Unknown action, missing identifier, missing/ill-typed `count` or `apply_immediately` |
| `node_id_pinned` | 409 | The fleet pins `EDGE_NODE_ID`, so a new node could never prove its own identity |
| `no_source_node` | 409 | No healthy, running fleet member is available to clone |
| `not_registered` | 409 | That instance is not registered behind any load balancer |
| `last_healthy_target` | 409 | It is the only healthy target of some attached group. `data.target_group` names it |
| `cannot_remove_self` | 409 | It is the node answering this request. `data.self_check` carries the check's status |
| `not_drained` | 409 | Terminate was asked for before the drain finished. `data.states` lists what the group reports |
| `resource_not_found` | 404 | AWS reports no such database or cache group |
| `not_a_reader` | 409 | The target is a primary database or an Aurora writer, not a reader |
| `not_a_source` | 409 | `add_reader` was pointed at a replica; point it at the source |
| `cluster_mode_unsupported` | 409 | Cluster-mode-enabled group; its replica count is a resharding decision |
| `automatic_failover_requires_replica` | 409 | Failover or Multi-AZ is on, so the group must keep at least one replica |
| `no_change` | 409 | The group already has that many replicas |
| `capacity_in_progress` | 409 | Another capacity change holds the single-flight claim. **All adds share one claim** |
| `cache_unavailable` | 503 | The coordination cache cannot rule out a concurrent change. Retry shortly |
| `dispatch_failed` | 503 | No job runner accepted the operation. **Nothing was changed** |
| `provider_denied` | 403 | AWS refused. `data.failure.iam_action` names the missing grant — and nothing else |
| `provider_unavailable` | 503 | A retryable AWS failure |
| `provider_error` | 502 | Any other AWS failure |
| `operation_not_found` | 404 | Unknown operation id (status only) |

### On a failed operation

| `error_code` | What the world looks like |
|---|---|
| `image_timeout` / `image_failed` | The AMI never became available. **Nothing was launched** |
| `launch_failed` / `launch_timeout` | The instance exists but never reached `running`. It is **not** registered |
| `runner_missing` | Running, but never joined the job fleet. **Not registered** — it is serving nothing |
| `no_converged_deployment` | The node joined, but no deployment has ever converged here, so there is no proven commit to install. **Not registered** |
| `proof_timeout` | The node did not prove it runs the fleet's commit. **Deliberately not registered** |
| `never_healthy` | Registered, but the balancer never reported it healthy. Drain it and investigate |
| `drain_timeout` | Still draining past the group's deregistration delay. **Not terminated** |
| `terminate_timeout` | AWS accepted the termination but the instance has not reached `terminated` |
| `reader_timeout` / `delete_timeout` | AWS accepted the change but the instance has not settled. It is billable — check the RDS console |
| `cache_timeout` | The group did not settle at the requested replica count |
| `operation_failed` | An unexpected error. Re-read `/api/aws/capacity` before retrying |

### Warning codes (non-fatal, on the operation)

| `code` | Meaning |
|---|---|
| `topology_not_updated` | The node is serving, but `EDGE_EXPECTED_TOPOLOGY` could not be extended. The message names the node to add by hand |
| `convergence_not_triggered` | The node is serving, but the vhost convergence sweep could not be queued. The scheduled sweep will pick it up |

## Two things to tell the operator

These are not decoration — they are the two places a reasonable person assumes
something untrue.

**A reader is standby capacity, not a speed-up.** django-mojo does not consume
a reader endpoint: `DATABASES` points at one host and every query goes there.
Adding a reader gives you an endpoint string; nothing gets faster until the
project wires it in. The completed operation's `message` carries the endpoint.

**A cache replica is failover capacity, not read throughput.** django-mojo
talks to the primary endpoint only. And removing the last replica of a
failover-off group leaves nothing to fail over to — say so before doing it.

One more, right after an add: fleet readiness reads `pending` until the
convergence sweep finishes. That is expected, not a fault.

## Related

- [aws/maintenance](maintenance.md) — engine-version upgrades on the same page family
- [aws/infrastructure_mode](infrastructure_mode.md) — how a client learns the installation's mode
