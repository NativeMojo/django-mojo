# Capacity actions

Add or remove an app node, an RDS reader, or an ElastiCache replica — resize
the cache group or a single database instance to a curated size — and turn
the fleet's stable outbound IPs on or off. Several changes can be reviewed
and executed as ONE server-validated batch (see [Batch plans](#batch-plans)).

```
GET  /api/aws/capacity            the fleet, reader and replica picture
GET  /api/aws/capacity/status     ?operation= one operation's progress · ?batch= one batch's progress
POST /api/aws/capacity/apply      request ONE capacity change
POST /api/aws/capacity/plan       validate, word, order and price a batch of changes
POST /api/aws/capacity/plan/apply confirm a plan by id; its steps run as one batch
```

Every apply is asynchronous: it returns an **operation** (or a **batch**)
immediately and the work is proven by polling `/status`. Nothing here reports
success on "AWS accepted it".

## Permissions

| Endpoint | Needs |
|---|---|
| `GET /api/aws/capacity` | `manage_aws` |
| `GET /api/aws/capacity/status` | `manage_aws` |
| `POST /api/aws/capacity/apply` | `manage_aws` **AND** a literal superuser, an interactive (non-key) session, and fresh auth within 600s |
| `POST /api/aws/capacity/plan` | same as `/apply` — a plan reveals intent, topology and cost, so it fails closed even though it mutates nothing |
| `POST /api/aws/capacity/plan/apply` | same as `/apply` |

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
    "schema_version": 2,
    "region": "us-east-1",
    "mode": "managed",
    "identity": {"project": "mojo", "environment": "prod"},
    "identity_available": true,
    "generated_at": "2026-08-18T18:00:00+00:00",
    "node_id_pinned": false,
    "nodes": {
      "balancers": [{"name": "mojo-api-nlb", "arn": "...", "type": "network", "state": "active"}],
      "groups": [{"arn": "...", "name": "mojo-api", "target_type": "instance", "port": 443, "protocol": "TCP"}],
      "instances": [
        {"id": "i-0a1b…", "name": "mojo-api-a", "state": "healthy",
         "instance_state": "running", "lifecycle_state": "available",
         "instance_type": "m6i.large", "registered": true, "can_drain": true,
         "zone": "us-east-1a", "subnet_id": "subnet-0aaa",
         "public_ip": "203.0.113.10", "healthy": true,
         "self": true, "primary": true, "added_by_capacity": false,
         "stable_ip": true, "groups": ["arn:…targetgroup/mojo-api/…"]}
      ],
      "self": "i-0a1b…",
      "self_check": "matched", "serving_available": true,
      "inventory_available": true
    },
    "databases": [
      {"identifier": "mojo-prod-aurora", "kind": "aurora", "engine": "aurora-postgresql",
       "status": "available", "writer": "mojo-prod-aurora-1",
       "readers": ["mojo-prod-aurora-2"],
       "writer_instance_class": "db.r6g.xlarge",
       "reader_instance_classes": {"mojo-prod-aurora-2": "db.t4g.medium"},
       "reader_statuses": {"mojo-prod-aurora-2": "creating"},
       "members": [
         {"id": "mojo-prod-aurora-1", "role": "writer", "status": "available",
          "lifecycle_state": "available", "instance_class": "db.r6g.xlarge",
          "can_resize": true, "can_remove": false},
         {"id": "mojo-prod-aurora-2", "role": "reader", "status": "creating",
          "lifecycle_state": "creating", "instance_class": "db.t4g.medium",
          "can_resize": false, "can_remove": false}
       ],
       "blocked_reason": "resource_transitioning",
       "reader_endpoint": "…cluster-ro-….rds.amazonaws.com",
       "endpoint": "…cluster-….rds.amazonaws.com"}
    ],
    "egress": {
      "enabled": true,
      "available": true,
      "fleet_available": true,
      "addresses_available": true,
      "policy_available": true,
      "fallback_attached": [],
      "addresses": ["203.0.113.10", "203.0.113.11"],
      "attached": [
        {"instance": "i-0a1b…", "public_ip": "203.0.113.10",
         "allocation_id": "eipalloc-…", "managed": true}
      ],
      "pending_nodes": [],
      "reserved": [{"allocation_id": "eipalloc-…", "public_ip": "203.0.113.20"}],
      "to_allocate": 0,
      "monthly_usd_per_address": 3.6
    },
    "caches": [
      {"identifier": "mojo-prod-redis", "status": "available", "replica_count": 1,
       "cluster_enabled": false, "automatic_failover_on": true, "multi_az_on": true,
       "node_type": "cache.t4g.micro", "resize_impact": "rolling",
       "members": [
         {"id": "…-001", "role": "primary", "status": "available", "lifecycle_state": "available"},
         {"id": "…-002", "role": "replica", "status": "available", "lifecycle_state": "available"}
       ],
       "min_replicas": 1, "blocked_reason": null}
    ],
    "sizes": {
      "cache": [
        {"size": "small",  "label": "Small",       "type": "cache.t4g.micro",  "monthly_usd": 12.0},
        {"size": "medium", "label": "Medium",      "type": "cache.t4g.medium", "monthly_usd": 50.0},
        {"size": "large",  "label": "Large",       "type": "cache.r7g.large",  "monthly_usd": 120.0},
        {"size": "xlarge", "label": "Extra large", "type": "cache.r7g.xlarge", "monthly_usd": 240.0}
      ],
      "database": [
        {"size": "small",  "label": "Small",       "type": "db.t4g.medium",  "monthly_usd": 50.0},
        {"size": "medium", "label": "Medium",      "type": "db.r6g.large",   "monthly_usd": 175.0},
        {"size": "large",  "label": "Large",       "type": "db.r6g.xlarge",  "monthly_usd": 350.0},
        {"size": "xlarge", "label": "Extra large", "type": "db.r6g.2xlarge", "monthly_usd": 700.0}
      ]
    },
    "warnings": [
      {"code": "databases", "iam_action": "rds:DescribeDBClusters",
       "aws_code": "AccessDenied",
       "message": "rds:DescribeDBClusters did not answer, so this section could not be read"}
    ],
    "reader_routing": {
      "database": {"active": true,
                   "host": "mojo-prod-aurora.cluster-ro-abc.us-east-1.rds.amazonaws.com",
                   "skip_reason": null, "matches_reader_endpoint": true},
      "redis": {"active": true}
    },
    "actions": {
      "add_node":           {"offered": true,  "blocked_reason": null},
      "drain_node":         {"offered": false, "blocked_reason": "last_healthy_target"},
      "terminate_node":     {"offered": true,  "blocked_reason": null},
      "add_reader":         {"offered": true,  "blocked_reason": null},
      "remove_reader":      {"offered": false, "blocked_reason": "no_reader"},
      "set_cache_replicas": {"offered": true,  "blocked_reason": null},
      "resize_cache":       {"offered": true,  "blocked_reason": null},
      "resize_database":    {"offered": true,  "blocked_reason": null},
      "enable_stable_ips":  {"offered": false, "blocked_reason": "already_enabled"},
      "disable_stable_ips": {"offered": true,  "blocked_reason": null}
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

`identity` is the provision declaration used to scope AWS discovery. With it,
EC2 nodes, RDS clusters/instances, ElastiCache groups, serving targets, and the
balancer-less address fallback are admitted only by exact project/environment
and role tags. A similarly named resource is not ownership proof.
If a declaration exists but is ambiguous or invalid, `identity_available` is
false, every action is blocked `identity_unavailable`, the warning names the
configuration problem, and the server performs no broad AWS inventory reads.
Only a project with no environment declaration at all retains the legacy
serving-only behavior.

Node inventory is independent of load-balancer registration. An owned pending,
running, stopping, stopped, or shutting-down EC2 instance remains in
`nodes.instances`; `registered` tells whether ELB currently contains it,
`instance_state` preserves AWS's word, and `lifecycle_state` normalizes it to
`available`, `creating`, `deleting`, or `error`. Render `can_drain` exactly as
returned. A balancerless running node is visible but not drainable.

`warnings` carries a degraded section. A section that could not be read comes
back empty **and** named here — never silently empty.

**`sizes` is the curated resize allowlist**, with the approximate monthly
on-demand cost per node beside each rung. Render the resize dropdowns from
exactly this — never a hardcoded list: the ladder can be re-pointed at new
instance families server-side without any client change. The current type per
row comes with it: cache rows carry a group-wide `node_type` (ElastiCache has
no per-member sizing — replicas always match their primary) plus
`resize_impact` (`"rolling"` — failover with at least one replica, replicas
replaced first then a brief failover — or `"downtime"` — no replica, the
cache is down while its node is replaced), stated **before** apply so the UI
can warn honestly. Aurora rows carry `writer_instance_class` and
`reader_instance_classes` (per reader); standalone rows already carried
`instance_class` and now fold each replica's class into
`reader_instance_classes` too.

Database and cache members also carry their own provider `status` and
normalized `lifecycle_state`. Database members add `can_resize` and
`can_remove`; render controls only from those fields. A parent cluster/group
may say `available` while one member is still creating or deleting, so the
parent status must never be copied onto its children. `blocked_reason:
"resource_transitioning"` disables changes for the affected resource until a
fresh read reports every member available.

**`reader_routing` is the serving process's self-report** on whether reader
traffic is actually configured — the settings behind it are file-only and read
at boot, so the process itself is the only honest source. `database.active`
means the reader alias and router are live in this process;
`database.skip_reason` surfaces a config line that was present but could not
be applied; `database.matches_reader_endpoint` compares the configured host
against the Aurora cluster reader endpoint AWS reports (`null` when there is
nothing to compare — unknown, never a false alarm). `redis.active` means a
standalone Redis reader is configured (always `false` in cluster mode, where
the cluster client routes replica reads itself). The answer is **per node**:
it describes the node that served this request, and a node that has not
restarted since the config changed still runs without routing.

**`egress` is the stable-outbound-IPs picture.** `addresses` is the canonical
list an operator hands to providers that allowlist caller IPs — the Elastic IPs
actually attached to fleet nodes, with unmanaged ones labelled
(`managed: false`) rather than hidden. `enabled` is the durable policy;
`pending_nodes` are registered nodes still without an address (re-running
enable converges exactly those); `reserved` are kept, unattached addresses a
future enable reuses first. `available` requires BOTH the serving read and the
addresses read: when either failed, the fleet is **unknown, not empty**, and a
client must render the list as unavailable — never as an empty allowlist. The
policy read gets the same honesty: when `policy_available` is false, `enabled`
is unknown — render "Unknown", never "off" — and both actions come back
blocked `policy_unavailable`.

**Balancer-less installs** get a read-only answer instead of a dead end: when
the serving read succeeds but no instance is registered behind any balancer,
`fallback_attached` lists Elastic IPs attached to an **owned** EC2 instance —
`{instance, instance_name, public_ip, allocation_id, managed}` — so a
single-node estate still shows the address to give providers. These rows are
report-only: they never feed the allowlist in `addresses`, never make either
action available, and the panel renders them "managed outside this portal". Cost
has a sign worth stating correctly: AWS bills every public IPv4 identically,
so an ATTACHED stable address replaces the node's auto-assigned-IPv4 charge
(enable is net ~zero/month), and the additive `monthly_usd_per_address`
appears at disable, when each kept reservation bills beside the node's new
auto-assigned address.

## `POST /api/aws/capacity/apply`

Common fields:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `action` | string | yes | One of `add_node`, `drain_node`, `terminate_node`, `add_reader`, `remove_reader`, `set_cache_replicas`, `resize_cache`, `resize_database`, `enable_stable_ips`, `disable_stable_ips` |
| `resource` | string | yes, except fleet-wide actions | Instance id, database identifier, or replication-group id |
| `confirm_resource` | string | yes | Must equal `resource` EXACTLY. The fleet-wide actions (`add_node`, `enable_stable_ips`, `disable_stable_ips`) have no resource — for them it is the literal action word |

Per-action fields:

| Action | Extra fields |
|---|---|
| `add_node` | `source_instance` (optional instance id) — the node to clone, which also decides **which balancer's fleet grows**: the clone is registered into exactly the target groups its source is in. Must be a healthy serving member of this fleet. `subnet_id` (optional) — which subnet the clone lands in. It must be in the **same availability zone as the source**; a fleet cannot be spread across zones (one zone, for now). Omit both for the historical behaviour: the server picks a healthy non-primary member and lands the clone in that member's own subnet |
| `set_cache_replicas` | `count` (integer, **no default**), `apply_immediately` (boolean, **no default**, must be `true`) |
| `resize_cache` | `size` (one of `small`/`medium`/`large`/`xlarge`, **no default**), `apply_immediately` (boolean, **no default**, must be `true`). `resource` is the replication-group id; the whole group is resized — node type is group-wide |
| `resize_database` | `size` (same four keys, **no default**), `apply_immediately` (boolean, **no default**, must be `true`). `resource` is ONE instance identifier — an Aurora writer, an Aurora reader, a standalone primary, or a standalone replica. The writer and the readers carry independent sizes |
| `enable_stable_ips` | `assign` (optional object `{instance_id: allocation_id}`) — give a NAMED eligible reservation to a named node instead of letting the server pick. An allocation is eligible only if it is unassociated **and** carries a django-mojo ownership tag; anything else is refused (`address_not_eligible`) before any mutation |

`confirm_resource` is checked **before any provider call**. `count`, `size`
and `apply_immediately` have no defaults: a missing field is a question that
was never asked, not a "no". `size` is a curated KEY from the report's
`sizes` block, never a raw instance type — a type string is refused
(`invalid_request`) before any provider call. Resizing an Aurora member also
sets its failover `PromotionTier` in the same call (writer 0, readers 1), so
a failover prefers the writer-class box; it never moves the writer role
itself.

`add_node`'s two placement fields do **not** change the confirmation
contract: `resource` stays absent (the server derives the targets, and
honouring a caller-supplied resource would let the caller choose their own
echo) and `confirm_resource` stays the literal string `add_node`. A named
subnet is validated against AWS **before** the claim and before the 20–40
minute AMI capture — it must exist, sit in the source's VPC, sit in the
source's **own availability zone**, and assign public addresses if the
source's subnet does. Sending either field on a `drain_node` or
`terminate_node` is `invalid_request` 400, not silently ignored.

### Bodies

```json
{"action": "add_node", "confirm_resource": "add_node"}

{"action": "add_node", "confirm_resource": "add_node",
 "source_instance": "i-0a1b…", "subnet_id": "subnet-0bbb"}

{"action": "drain_node", "resource": "i-0a1b…", "confirm_resource": "i-0a1b…"}

{"action": "terminate_node", "resource": "i-0a1b…", "confirm_resource": "i-0a1b…"}

{"action": "add_reader", "resource": "mojo-prod-aurora",
 "confirm_resource": "mojo-prod-aurora"}

{"action": "remove_reader", "resource": "mojo-prod-aurora-2",
 "confirm_resource": "mojo-prod-aurora-2"}

{"action": "set_cache_replicas", "resource": "mojo-prod-redis",
 "confirm_resource": "mojo-prod-redis", "count": 2, "apply_immediately": true}

{"action": "resize_cache", "resource": "mojo-prod-redis",
 "confirm_resource": "mojo-prod-redis", "size": "large", "apply_immediately": true}

{"action": "resize_database", "resource": "mojo-prod-aurora-2",
 "confirm_resource": "mojo-prod-aurora-2", "size": "medium", "apply_immediately": true}

{"action": "enable_stable_ips", "confirm_resource": "enable_stable_ips"}

{"action": "enable_stable_ips", "confirm_resource": "enable_stable_ips",
 "assign": {"i-0c3d…": "eipalloc-0123…"}}

{"action": "disable_stable_ips", "confirm_resource": "disable_stable_ips"}
```

### Response

```json
{
  "status": true,
  "data": {
    "schema_version": 2,
    "id": "9f1c…",
    "action": "add_node",
    "resource": "i-0a1b…",
    "state": "running",
    "phase": "capturing",
    "phases": ["capturing", "launching", "booting", "converging",
               "proving", "addressing", "registering", "settling"],
    "message": "requested",
    "error_code": null,
    "warnings": [],
    "detail": {"source_instance": "i-0a1b…", "source_name": "mojo-api-a",
               "source_selected": "requested", "subnet_id": "subnet-0bbb",
               "subnet_selected": "requested", "availability_zone": "us-east-1a"}
  }
}
```

`id` is what you poll.

On an `add_node`, `detail` always states the full placement it settled on:
`source_selected` and `subnet_selected` are `"requested"` when the caller
named them and `"automatic"` / `"source"` when the server chose. `subnet_id`
and `availability_zone` are where the node will actually land.

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
| `add_node` | `capturing` → `launching` → `booting` → `converging` → `proving` → `addressing` → `registering` → `settling` (`addressing` runs only while stable outbound IPs are on) |
| `drain_node` | `draining` |
| `terminate_node` | `terminating` |
| `add_reader` | `creating` → `settling` |
| `remove_reader` | `deleting` |
| `set_cache_replicas` | `scaling` → `settling` |
| `resize_cache` | `resizing` → `settling` |
| `resize_database` | `resizing` → `settling` |
| `enable_stable_ips` | `planning` → `associating` → `verifying` |
| `disable_stable_ips` | `detaching` → `verifying` |

Terminal operations report `phase: "complete"`.

## Batch plans

Two phases. `POST /capacity/plan` validates an ordered list of steps against
the fleet report and answers with the plan **in the server's own words** —
per-step description, warnings, cost delta, and a safe execution order.
`POST /capacity/plan/apply` confirms that exact plan by `plan_id` and runs
the steps sequentially as ordinary capacity operations, each re-checked
against live AWS the moment it runs.

There is **no per-step `confirm_resource`** in the batch flow, by design:
confirming the `plan_id` is confirming exactly the step list the server
itself rendered.

### `POST /api/aws/capacity/plan`

```json
{
  "steps": [
    {"action": "add_node"},
    {"action": "add_node", "source_instance": "i-0a1b…",
     "subnet_id": "subnet-0bbb"},
    {"action": "resize_database", "resource": "mojo-prod-aurora-2",
     "size": "medium", "apply_immediately": true},
    {"action": "set_cache_replicas", "resource": "mojo-prod-redis",
     "count": 2, "apply_immediately": true},
    {"action": "drain_node", "resource": "i-0a1b…"},
    {"action": "terminate_node", "resource": "i-0a1b…"}
  ]
}
```

Accepted actions: `add_node`, `add_reader`, `set_cache_replicas`,
`resize_cache`, `resize_database`, `remove_reader`, `drain_node`,
`terminate_node` — at most 20 steps. The stable-IPs pair is refused with its
own message: it is fleet-wide, holds its own claim, and runs alone through
the single-action apply. Per-step params match the single-action apply
(`count`/`size`/`apply_immediately`, same no-default rules; `add_node`'s
optional `source_instance` and `subnet_id`).

Validation refuses, naming the step index in `data.step`: unknown actions or
resources, the self node, duplicates, counts under the failover floor, a
resize to the current size, a `terminate_node` with no `drain_node` for the
same node earlier in the batch (unless its drain already completed), drains
that would leave no healthy node serving (only drains of currently-healthy
nodes count against that budget), and a batch that both resizes and removes
the same resource (`conflicting_steps`).

`add_node`'s placement splits: `source_instance` is checked against the
report's healthy rows here (`source_not_serving` 409), while `subnet_id` is
shape-checked only — an empty subnet holds no node by definition, so no report
could validate it, and the step's own apply proves it against AWS (VPC, zone,
addressing) before it takes a claim. Two `add_node` steps naming different
subnets are both accepted.

```json
{
  "status": true,
  "data": {
    "schema_version": 2,
    "id": "5b0c…",
    "created": "2026-08-20T18:00:00+00:00",
    "expires_in": 300,
    "expires_at": "2026-08-20T18:05:00+00:00",
    "actor": 1,
    "steps": [
      {"index": 0, "action": "add_node", "resource": "",
       "params": {"source_instance": "i-0a1b…", "subnet_id": "subnet-0bbb"},
       "kind": "add",
       "description": "Add an app node cloned from mojo-api-a in subnet-0bbb",
       "warnings": ["builds, deploys and proves itself before serving · 20–40 min"],
       "monthly_delta_usd": 70.0},
      {"index": 1, "action": "resize_database", "resource": "mojo-prod-aurora-2",
       "params": {"size": "medium", "apply_immediately": true}, "kind": "change",
       "description": "Resize reader mojo-prod-aurora-2 to db.r6g.large",
       "warnings": ["reads keep flowing; this reader pauses while it changes class"],
       "monthly_delta_usd": 125.0}
    ],
    "total_monthly_delta_usd": 195.0,
    "estimate_complete": true,
    "order_note": "Steps run in the server's order: additions first, then resizes, then removals — and a terminate runs immediately after its own drain."
  }
}
```

Render the steps **as returned**: the server has already reordered them
(additions → resizes → removals, each `terminate_node` immediately after its
own `drain_node`) regardless of submission order, and `order_note` says so.
`kind` (`add`/`change`/`remove`) is for styling only.

**The null-cost convention**: a step whose instance type has no listed price
answers `monthly_delta_usd: null` plus a warning (`"no listed price for …"`),
and the plan carries `estimate_complete: false`; the total sums only priced
steps. Never render a null as $0.

Plans validate against the server's cached report (≤2 minutes old) so the
review loop can re-plan on every tweak; the apply below takes a fresh sweep.
A degraded AWS read that touches a planned section answers **503
`report_degraded`** — retry, it is never "unknown resource".

### `POST /api/aws/capacity/plan/apply`

```json
{"plan_id": "5b0c…"}
```

The server re-reads AWS fresh and compares a structural fingerprint of the
fleet against the one the plan was written from:

- plan expired or unknown → **404 `plan_not_found`** ("plans expire after 5
  minutes — re-plan"). Request a new plan and show it again — never silently
  re-plan-and-apply.
- fleet changed since the plan → **409 `plan_stale`**. Same remedy.
- plan already applied → **409 `plan_already_applied`**, with
  `data.batch` naming the running batch — a double-click converges on
  polling, not on a second batch.

On success the response is the **batch record** (same shape as the status
below, all steps `pending` except the bookkeeping), and one background job
starts walking the steps.

### `GET /api/aws/capacity/status?batch=<id>`

Exactly one of `operation` and `batch` per request (both or neither is a
400). Poll roughly every 10 seconds:

```json
{
  "status": true,
  "data": {
    "schema_version": 2,
    "id": "9c2f…",
    "plan_id": "5b0c…",
    "actor": 1,
    "state": "running",
    "started": "2026-08-20T18:05:03+00:00",
    "current_index": 1,
    "message": "step 2 of 3: Resize reader mojo-prod-aurora-2 to db.r6g.large",
    "error_code": null,
    "steps": [
      {"index": 0, "action": "add_node", "resource": "", "params": {},
       "kind": "add", "description": "Add an app node", "state": "done",
       "operation": "9f1c…", "phase": "complete",
       "message": "i-0c3d… is serving as mojo-api-a-0c3d…", "error_code": null},
      {"index": 1, "action": "resize_database", "resource": "mojo-prod-aurora-2",
       "params": {"size": "medium", "apply_immediately": true},
       "kind": "change", "description": "Resize reader … to db.r6g.large",
       "state": "running", "operation": "8e2a…", "phase": "settling",
       "message": "mojo-prod-aurora-2 is modifying", "error_code": null},
      {"index": 2, "action": "drain_node", "resource": "i-0a1b…", "params": {},
       "kind": "remove", "description": "Drain mojo-api-b — traffic moves off it first",
       "state": "pending", "operation": null, "phase": null,
       "message": null, "error_code": null}
    ],
    "updated": "2026-08-20T18:07:41+00:00",
    "stalled": false
  }
}
```

Step `state`: `pending` → `running` → `done` / `failed` / `not_attempted`.
Each step also carries the `params` it will run (or ran) with — same shape as
the plan's step object. Each step's `operation` is a full child operation id
— pollable via `?operation=` for the complete detail. The runner mirrors the
child's `phase`/`message` onto the step, so one batch poll answers
everything. `started` is when the batch began; `updated` is the last write —
`stalled` (below) is derived from how stale it is.

**Mid-batch failure**: the failed step carries the child's `error_code` and
message, every later step is `not_attempted`, the batch is `failed`, and its
`message` says exactly where things stand ("Step N of M failed: …; the
remaining K step(s) were not attempted."). **Nothing is rolled back** — the
completed steps happened. Two failure codes exist only on a batch step, never
on a plain operation: `operation_vanished` (the child operation's record
disappeared from the coordination cache mid-poll — check the AWS console; the
operation id is in the message) and `operation_timeout` (the child gave no
terminal answer within the batch's backstop ceiling and may still be working
— poll `?operation=` and check the AWS console). Both land as the step's
(and the batch's) `error_code` inside an ordinary `200` status response —
neither is an HTTP error of its own.

`stalled: true` on a running batch means it has reported no progress for a
few minutes — the runner thread is gone (job-engine death or a deploy
mid-batch). Nothing auto-resumes; show "check the jobs runner". An unknown
batch id is **404 `batch_not_found`** (batch records live in the
coordination cache). `stalled` is computed by `/status` only — the immediate
`POST /plan/apply` response (above) does not carry it.

## Error codes

Refusals return HTTP `4xx`/`5xx` with
`{"status": false, "error": "...", "error_code": "...", "data": {...}}`.
Failures **after** the apply was accepted appear as `error_code` on the
operation record instead.

### From the apply

| `error_code` | HTTP | Meaning |
|---|---|---|
| `infrastructure_external` | 403 | `INFRASTRUCTURE_MODE=external`. No caller may change capacity here. `data` carries `mode` and `setting` |
| `invalid_request` | 400 | Unknown action, missing identifier, missing/ill-typed `count` or `apply_immediately`, a `subnet_id` that is not a `subnet-…` identifier, or `source_instance`/`subnet_id` sent on a `drain_node` or `terminate_node` (they are `add_node`'s placement and are refused elsewhere, never ignored) |
| `identity_unavailable` | 503 | A selected, malformed, or ambiguous provision environment prevents exact ownership proof. Correct the declaration; capacity performs no account-wide fallback |
| `node_id_pinned` | 409 | The fleet pins `EDGE_NODE_ID`, so a new node could never prove its own identity |
| `no_source_node` | 409 | No healthy, running fleet member is available to clone |
| `source_not_serving` | 409 | `add_node`'s `source_instance` is not a healthy target of this fleet's balancers; or it is a healthy target AWS does not report running; or this installation has declared no AWS environment, in which case no named source can be proven to belong to this fleet (add the node without naming one) |
| `subnet_not_found` | 404 | AWS reports no such subnet in this region |
| `subnet_not_usable` | 409 | The named subnet cannot take this clone. `data.reason` is `vpc_mismatch` (a clone carries its source's VPC-scoped security groups), `az_mismatch` (the subnet is in a different availability zone than the source — a fleet is one zone for now, and `data.zone` / `data.source_zone` name both sides), `no_public_addressing` (the source's subnet assigns public addresses and this one does not, so the clone would never reach anything), or `no_free_addresses` |
| `not_registered` | 409 | A **drain** named an instance not registered behind any load balancer. (Terminate no longer refuses this shape — see `not_fleet_member`) |
| `not_fleet_member` | 409 | A terminate named an unregistered instance that fresh EC2 facts could not prove a member of THIS fleet: missing, no admin-capacity clone stamp and no django-mojo tags, `mojo:project`/`mojo:env` not exactly matching a currently registered member's, or no registered member left to verify identity against (fail closed — use the console). A COMPLETED drain removes the target from its group, so terminate proves membership by identity, never by a generic tag |
| `already_terminated` | 409 | The unregistered instance is already `terminated`/`shutting-down` |
| `last_healthy_target` | 409 | It is the only healthy target of some attached group. `data.target_group` names it |
| `cannot_remove_self` | 409 | It is the node answering this request. `data.self_check` carries the check's status |
| `not_drained` | 409 | Terminate was asked for before the drain finished. `data.states` lists what the group reports |
| `resource_not_found` | 404 | AWS reports no such database or cache group |
| `not_a_reader` | 409 | The target is a primary database or an Aurora writer, not a reader |
| `not_a_source` | 409 | `add_reader` was pointed at a replica; point it at the source |
| `cluster_mode_unsupported` | 409 | Cluster-mode-enabled group; its replica count (and its sizing) is a resharding decision. Refuses `set_cache_replicas` and `resize_cache` alike |
| `automatic_failover_requires_replica` | 409 | Failover or Multi-AZ is on, so the group must keep at least one replica |
| `no_change` | 409 | The group already has that many replicas, a resize named the size the resource already runs, or a disable found nothing to detach |
| `not_settled` | 409 | A resize was asked of a resource that is not settled. The message quotes the provider's state verbatim (`modifying`, `deleting`, …) and attributes nothing — the state may be AWS background work. Routine background states are allowed through: cache `snapshotting`; RDS `backing-up`, `storage-optimization`, `maintenance` |
| `resource_transitioning` | 409 | The fresh report shows a database/cache parent or member creating, deleting, modifying, or otherwise not available. Refresh and inspect; the control is not safe yet |
| `no_fleet_nodes` | 409 | No node is registered behind a load balancer, so there is nothing to give a stable address to |
| `address_not_eligible` | 409 | An explicitly assigned allocation is associated, unknown, or carries no django-mojo ownership tag. The remedy (tag it in the console) is in the message |
| `capacity_in_progress` | 409 | Another capacity change holds the single-flight claim. **All adds share one claim; enable and disable of stable IPs share another; a cache resize and a replica-count change on the same group share that group's claim** |
| `cache_unavailable` | 503 | The coordination cache cannot rule out a concurrent change, **or** the operation record could not be recorded at request time. Either way **nothing was started** — an operation nobody can record is one the runner would never find. Retry shortly. The stable-IPs actions say more: their durable policy IS recorded, and no address was attached or detached |
| `dispatch_failed` | 503 | No job runner accepted the operation. **Nothing was changed** — except for the stable-IPs actions, where the durable policy WAS recorded before dispatch; their message says so, and re-running the action converges |
| `provider_denied` | 403 | AWS refused. `data.failure.iam_action` names the missing grant — and nothing else |
| `provider_unavailable` | 503 | A retryable AWS failure |
| `provider_error` | 502 | Any other AWS failure |
| `mutation_state_unknown` | 502 | AWS may have accepted a write whose response could not be confirmed. The claim stays held; refresh the report and reconcile provider state. **Do not replay the mutation** |
| `operation_not_found` | 404 | Unknown operation id (status only) |
| `conflicting_steps` | 409 | A batch both resizes and removes the same resource. `data.step` names the step (plan only) |
| `report_degraded` | 503 | An AWS read touching a planned section did not answer completely — retry; never rendered as "unknown resource" (plan and plan/apply) |
| `plan_not_found` | 404 | Unknown or expired plan id — plans live 5 minutes. Re-plan (plan/apply only) |
| `plan_stale` | 409 | The fleet changed since the plan was written. Re-plan; never silently re-applied (plan/apply only) |
| `plan_already_applied` | 409 | This plan already started a batch; `data.batch` names it — poll that instead (plan/apply only) |
| `batch_dispatch_failed` | 503 | No job runner accepted the batch. **Nothing was started** (plan/apply only) |
| `batch_not_found` | 404 | Unknown batch id (status only) |

Plan-time refusals reuse the single-action codes above (`resource_not_found`,
`cannot_remove_self`, `no_change`, `not_drained`, `last_healthy_target`,
`automatic_failover_requires_replica`, …) and carry `data.step` — the index
of the refused step.

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
| `resize_timeout` | The resource did not settle on the new size in time. The mutation is AWS-side — check the console the message names; the report re-derives the truth on its next read |
| `address_quota` | AWS's public-IPv4 quota is exhausted (default 5 per region). The message says how many nodes were addressed and how many remain; raise the quota and run the enable again |
| `address_failed` | An address could not be attached. On `add_node` the clone is running and **deliberately not registered** — a node whose egress nobody allowlisted must not serve |
| `address_unverified` | The verify re-read did not show the expected associations. Run the action again — both stable-ips operations converge only what is missing |
| `policy_unreadable` | `add_node` could not read the stable-IPs policy, so the node was **not registered** rather than admitted unaddressed |
| `persistence_unavailable` | Progress could not be recorded, so the operation stopped **BEFORE its next irreversible AWS change**. Nothing further was changed. While the cache is down this state may not be readable — check the AWS console and the `aws.log` line naming the operation id. The claim may survive to its 90-minute TTL, so a retry can answer `capacity_in_progress` until it expires |
| `mutation_state_unknown` | A provider write may have succeeded but its result could not be confirmed. Refresh and reconcile the provider inventory; **do not replay** |
| `operation_failed` | An unexpected post-acceptance failure leaves the outcome uncertain. Refresh and reconcile provider state; **do not replay** |

### Warning codes (non-fatal, on the operation)

| `code` | Meaning |
|---|---|
| `topology_not_updated` | The node is serving, but `EDGE_EXPECTED_TOPOLOGY` could not be extended. The message names the node to add by hand |
| `convergence_not_triggered` | The node is serving, but the vhost convergence sweep could not be queued. The scheduled sweep will pick it up |
| `node_not_running` | A registered instance was not running during an enable and was left without a stable address |

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

**Resizing interrupts something, and which thing must be said up front.**

- `resize_cache` on a group with automatic failover and at least one replica
  (`resize_impact: "rolling"`): replicas are replaced first, then a brief
  failover swaps the primary — one short interruption, not an outage. With no
  replica (`"downtime"`): the cache is down while its node is replaced. The
  report states which case applies **before** apply — render it on the
  control, not just in the confirmation.
- `resize_database` on a **writer** (Aurora writer or standalone primary):
  the instance restarts to change class — a few minutes offline. Say
  "~minutes offline", not "brief".
- `resize_database` on a **reader**: the writer keeps serving and reads keep
  flowing on the other readers; only this reader pauses while it changes
  class.
- Downsizing is allowed (the guard is identity, not direction). A smaller
  cache node risks eviction under the group's maxmemory policy if the working
  set no longer fits — the completed operation's `message` says so; the
  server states the risk without pretending to measure it.
- The zero-downtime Aurora writer recipe — resize a reader, fail over to it,
  resize the old writer — is still **not** one action. The batch API composes
  capacity steps, but failover is not a capacity action, so the middle of
  that recipe remains a manual AWS-console move.

**Enabling stable IPs costs ~nothing; disabling is what bills.** AWS charges
every public IPv4 the same, attached or not, so an attached Elastic IP just
replaces the node's auto-assigned-address charge. After a disable, the kept
reservations bill (~$3.60/month each) ON TOP of the nodes' new auto-assigned
addresses — say so, and say that releasing them is an AWS-console decision,
never part of the toggle.

**Disabling can break provider calls immediately, and recovery is not
instant.** The moment an address detaches, providers that allowlisted it may
refuse the fleet. Each node then gets a NEW auto-assigned address after a gap
of up to a few minutes — or no public address at all if its network interface
was not launched with auto-assign. The completed operation's `message` reports
each node's post-detach address.

## Related

- [aws/maintenance](maintenance.md) — engine-version upgrades on the same page family
- [aws/infrastructure_mode](infrastructure_mode.md) — how a client learns the installation's mode
