# Capacity actions

Adding and removing an app node, an RDS reader, or a cache replica — resizing
the cache group or a single database instance to a curated size — and turning
the fleet's stable outbound IPs on and off — from the Admin portal, with
server-derived guards, an audit trail, and proof that an added node runs the
code the rest of the fleet runs.

The alternative this replaces is an operator in the AWS console at 2am, adding
a node by hand and hoping they picked the same instance type, the same subnet,
the same security groups and the same instance profile, with no record of who
asked for it.

- Service: `mojo/apps/aws/services/capacity.py`
- Endpoints: `mojo/apps/aws/rest/capacity.py` (see the web-developer track)
- Helpers: `mojo/helpers/aws/{ec2,elbv2,rds,elasticache}.py`
- Jobs: `mojo.apps.aws.asyncjobs.capacity_operation` (one action) and
  `capacity_batch` (a plan's steps in sequence)
- Panels: `admin_portal/assets/features/platform/capacity.js` (Dashboard EC2
  drill-in, single actions) and `fleet.js` (Fleet Scaling, batch plan/apply)

## The architecture ruling this sits under

Portal-direct AWS control is the source of truth for installations in
**managed** mode. There is no IaC underneath it, and none is planned. On
installations whose estate IS applied by an external pipeline,
`INFRASTRUCTURE_MODE=external` disables every mutation here — see
[infrastructure_mode.md](infrastructure_mode.md). Reads stay available in both
modes: an install that does not own its estate still gets to see what it has.

**The first node of an installation is not this feature's job.** Bootstrapping
is the project's own provisioning. This adds the second and subsequent nodes by
cloning one that already works.

## Hostname is identity, everywhere

This is the single fact the whole add sequence is built on:

| Thing | Value |
|---|---|
| jobs runner id | `<hostname>-engine` (`jobs.job_engine.host_channel()` + `ENGINE_CHANNEL_SUFFIX`) |
| readiness node id | `<hostname>` (`edge.services.readiness.local_node_id()`) |
| certbot primary election | `hostname == PRIMARY_BALANCER_HOST` (`mojo/deploy/certbot_sync.py`) |

So a clone that kept its source's hostname would be a second job engine
claiming the source's runner id, a second node reporting the source's readiness
identity, and — if the source is the primary — a second box that thinks it
renews the fleet's certificates. The clone's user-data therefore sets a unique
hostname derived from its own instance id **before** anything else, and
restarts the two things that carry that identity.

Because the hostname decides all three, the server can **predict** the new
node's identity from the instance id `RunInstances` returned:

```python
node_id = capacity.expected_node_id(source_name, instance_id)   # <base>-<id suffix>
runner_id = capacity.expected_runner_id(node_id)                # + "-engine"
```

`expected_node_id` computes exactly what `"<base>-${IID##*-}"` expands to in the
user-data. That is what lets the join leg wait for **one named runner** instead
of diffing the roster and guessing which new heartbeat is the node it launched.

### The user-data

```bash
#!/bin/bash
set -u
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300" || true)
IID=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/instance-id" || true)
if [ -n "$IID" ]; then
  hostnamectl set-hostname "<base>-${IID##*-}"
fi
systemctl start config-sync.service || true
python3 -m mojo.deploy.node_setup --root "<root>" || true
systemctl restart mojo-asgi.service || true
"<root>/bin/jobman" restart || true
```

Five things, each load-bearing:

1. **IMDSv2 token first.** The launch forces `HttpTokens=required`, so an
   unauthenticated metadata read would simply fail.
2. **config-sync**, so the node pulls **its own** `django.conf` from S3 rather
   than running on whatever the AMI baked. The baked config is deliberately
   **not deleted** — a node whose config-sync fails and which has no config at
   all is an unbootable box, and the sync overwrites in place anyway.
3. **`node_setup`**, which is idempotent: it converges `var/` ownership, the
   systemd units and the jobs cron. See
   [deploy/README.md](../deploy/README.md).
4. **`mojo-asgi` restart.** Cloud-init runs LATE. The app service already
   started from the AMI, under the source's hostname.
5. **`jobman restart`.** Same reason, and this is the one that matters most:
   the job engine is what registers the runner id.

4 and 5 are best-effort (`|| true`). A fleet that names its units differently
loses the restart, not the boot — and the join leg's runner wait is what
notices.

`ADMIN_CAPACITY_NODE_ROOT` (default `/opt/api`) is the `--root` passed to
`node_setup`.

## Adding a node, end to end

`POST /api/aws/capacity/apply {action: "add_node"}` does the bounded part
synchronously — guards, single-flight claim, an operation record — and hands
the rest to `mojo.apps.aws.asyncjobs.capacity_operation`, which walks this
ladder and writes each phase onto the record:

| Phase | What happens | Failure |
|---|---|---|
| `capturing` | Reuse a `mojo:fleet-image` AMI younger than `ADMIN_CAPACITY_IMAGE_MAX_AGE_DAYS` (14), else `create_image(NoReboot=True)` on a healthy **non-primary** member and poll until `available` | `image_timeout` after 30 min — **nothing was launched** |
| `launching` | `run_instances` cloning instance type, subnet, security groups and instance profile, with `HttpTokens=required`, `mojo:created-by=admin-capacity`, and the source's identity tags (`mojo:project`/`mojo:env`/`managed-by`) so the clone is born a provable fleet member | `launch_timeout` — the instance exists and is NOT registered |
| `booting` | Wait for `<node_id>-engine` on the `edge` channel | `runner_missing` after 20 min — running, unregistered, serving nothing |
| `converging` | ONE targeted `_publish_deploy_node(runner_id, row.sha, row.framework_version, migrate=False, deployment_id=row.pk)` for `platform_deploy.last_converged_deployment()` | `no_converged_deployment` |
| `proving` | Poll `readiness.local_node_proof` over `execute_on_runner` until `platform_deploy.proof_matches` accepts it | `proof_timeout` — **NOT registered** |
| `addressing` | Only while stable outbound IPs are on: reuse a reserved `mojo:eip=stable-egress` address, else allocate one, associate it, before registration | `address_failed` / `address_quota` — **NOT registered**; `policy_unreadable` if the policy row cannot even be read |
| `registering` | `register_target` into every group the source is in, then extend `EDGE_EXPECTED_TOPOLOGY` | topology failure is a warning, never a failure |
| `settling` | Poll target health until healthy in every group, then trigger the combined pool convergence | `never_healthy` — the row offers Drain |

### Registration is gated on proof

This is the design's spine. A node is registered behind the balancer **only
after it reports the deployment uuid and sha of the fleet's last converged
deployment**. `proof_matches` requires both — a node reporting the right sha
under a different attempt's uuid does not count.

The trade is deliberate and one-sided: an unproven node costs money and serves
nothing, which is visible and cheap to fix (Drain, then Terminate). A node
registered without proof serves production traffic while running unknown code.

### `migrate=False`, and one channel

The converge publishes to the new runner's own box-direct channel
(`<runner_id>`), never `deploy.DEPLOY_CHANNEL`. Publishing on the fleet channel
would redeploy the whole fleet because one node was added — exactly the blast
radius this feature exists to avoid. `migrate=False` for the same reason: the
fleet is already on this sha, and the migration ran when it converged.

### Deploy evidence is dropped, by design

`_publish_deploy_node` records `platform_deploy.evidence(...)`, and
`evidence()` refuses any runner outside the deployment's **frozen roster**. The
new node is not in that roster — the roster was frozen when that deployment
ran. So the evidence write returns `False` and nothing is recorded on the
deployment row.

That is correct. Retroactively adding a node to a closed deployment's roster
would corrupt the record of what that deployment actually proved. The capacity
operation's own record and its audit event are the trail for this node.

### Topology extension

`EDGE_EXPECTED_TOPOLOGY` is a protected setting (`system_settings.set_value`,
superuser-only). The add **extends** its `nodes` list and never touches
`pools`. Two rules:

- A topology that is **not configured** is left unconfigured. Writing one where
  none existed would newly constrain a fleet that deliberately let readiness
  derive its own per-node view.
- A write that fails produces a `topology_not_updated` warning on the
  operation, not a failed add. A node that is serving traffic is serving
  traffic whether or not a settings row lists it; the warning names the node to
  add by hand.

### After the add

Fleet readiness reads `pending` until the convergence sweep completes. That is
expected, and the panel says so.

## Removing a node

**Drain and terminate are separate actions.** Draining takes a node out of the
serving path; the instance keeps running and keeps billing. Terminating
destroys it.

`drain_node` deregisters from every group holding the instance and polls until
drained, bounded by the group's own `deregistration_delay` plus a margin.

**`draining` and `unhealthy.draining` are in-flight, never done.** AWS reports
them for the whole deregistration delay. `elbv2.drained()` treats only `unused`
(or absence from the group) as drained, and `terminate_node` re-derives that
server-side from a fresh describe — a client saying "already drained" is a
client, not evidence.

**A COMPLETED drain removes the target from its group entirely**, so the node
terminate exists to destroy is invisible to the serving map. Terminate
therefore has two proof paths: still registered → every group must report it
drained (`not_drained` otherwise, unchanged); registered nowhere →
`_prove_fleet_member` re-describes the instance fresh, requires it to exist
and not already be `terminated`/`shutting-down` (`already_terminated`), then
proves identity one of two ways: the `mojo:created-by=admin-capacity` stamp
this feature puts on every clone it launches, **or** a django-mojo ownership
tag PLUS an exact identity match — the candidate's `mojo:project` and
`mojo:env` equal to those of a **currently registered** fleet member (the
identity anchor), a missing value on either side counting as a mismatch,
never a wildcard. A generic django-mojo tag alone is deliberately not
enough: a staging and a prod fleet in one account+region are both
django-mojo-tagged, and this predicate gates `TerminateInstances` — the
same reason provision's `spec.owns()` matches identity, never ownership
alone. No registered member to anchor against → refused outright (use the
AWS console). Anything else refuses `not_fleet_member`. The self check runs
against the bare resource id too (a drained node is absent from the serving
describe, and a drained self node must still not terminate itself). Without
this, drain→terminate could never complete — in a batch or in two manual
clicks. Clones launched here carry the source's identity tags from birth, so
they satisfy either proof.

### The two refusals

| Refusal | How it is derived |
|---|---|
| `last_healthy_target` | `elbv2.serving_map()` — a FRESH `describe_load_balancers` + one `describe_target_groups` + a `describe_target_health` per attached group, across EVERY balancer. Never the dashboard's 60-second cache. |
| `cannot_remove_self` | One `describe_instances`, comparing this box's short hostname against each instance's `PrivateDnsName` first label AND its `Name` tag. |

The self check reports **`self_check: "unavailable"`** when nothing matches —
never "this is not me". A fleet that sets its own hostnames will not match the
AWS-assigned DNS label, and absent evidence is not proof of safety. The
confirmation copy says so out loud and asks the operator to check the instance
id.


## Stable outbound IPs

Providers that allowlist caller IPs need the fleet's outbound addresses to
never change. `enable_stable_ips` / `disable_stable_ips` are fleet-wide
capacity actions on the same operation contract: single-flight claim, phased
job, audit event, and **no success until the association state is re-read from
AWS**.

### The policy is a protected system setting

`AWS_STABLE_OUTBOUND_IPS` (`{"enabled": bool}`), registered with its validator
by the aws app's `AppConfig.ready()`. It lives in the database behind
`system_settings.set_value` — superuser-only, refused on the generic settings
REST plane — so API and job processes on **any** node read the same durable
intent. The apply writes the policy in the request thread (the
human-authenticated superuser) and the job merely converges it: a failed job
leaves the report showing enabled-but-pending with the action still offered,
and re-running it converges exactly what is missing.

### Tags gate mutation; associations gate reporting

The report's `egress.addresses` — the canonical vendor allowlist — is what is
actually **attached** to fleet nodes, with unmanaged addresses labelled rather
than hidden. Mutation is narrower: this feature creates, renames, or detaches
only addresses carrying `mojo:eip=stable-egress`.

- **Allocate**: tagged at creation (`Name`, `mojo:eip=stable-egress`,
  `mojo:created-by=admin-capacity`) — an allocation that succeeded but could
  not be tagged would be invisible to every later reuse pass.
- **Reuse**: unassociated `stable-egress` reservations are consumed before
  anything new is allocated. "Unassociated" requires no association, no
  instance AND no network interface — an NLB's address has no instance id and
  is very much in use.
- **Adopt**: a node-attached address is claimed (tagged) only when it already
  carries django-mojo ownership tags (`managed-by=django-mojo` /
  `mojo:project`) — the pre-balancer provision case. An untagged attached
  address satisfies its node and appears in the allowlist, but is never
  tagged, renamed, or detached.
- **Explicit assignment**: the apply's optional `assign`
  (`{instance_id: allocation_id}`) hands a named eligible reservation to a
  named node. Eligible = unassociated + django-mojo-tagged; anything else is
  refused (`address_not_eligible`) before any mutation, with the remedy (tag
  it in the console) in the message.
- `associate_address` passes `AllowReassociation=False` **explicitly**: losing
  a race errors and re-plans once; stealing an address in production would be
  an outage.

### Enable, disable, and the add_node leg

**Enable** (`planning` → `associating` → `verifying`): fresh serving +
address reads, adopt by tag, then per pending node assigned → reserved →
allocated, and a final re-read proves every registered running node holds an
address before `done`. Registered-but-not-running instances are warned
(`node_not_running`) and skipped. **Disable** (`detaching` → `verifying`):
disassociates only `stable-egress` associations, keeps every allocation
(release is deliberately a console action, never a side effect), and the
finish message reports each node's post-detach address — recovery onto an
auto-assigned address is neither instant nor guaranteed.

**add_node** gains the `addressing` leg between proving and registering, and
it **fails closed**: a clone that cannot get its address is left running and
unregistered, exactly like a clone that cannot prove its commit — an
unregistered node costs money and serves nothing, while a registered node
egressing from a non-allowlisted address fails provider calls in production.
The leg reads the policy through a **raising** path
(`_egress_enabled(strict=True)`): `Setting.get_from_db` swallows every
exception into not-found, which is fine for a report and wrong for an
admission gate, so an unreadable policy fails the add (`policy_unreadable`)
rather than silently skipping the leg.

### Claims release on provider failure — deliberately unlike add_node

`run_operation`'s generic handler holds the claim whenever a mutation was
attempted, because a retried add is a second live instance. Both stable-ips
runners are **idempotent by construction** — satisfied nodes are skipped,
decisions re-derive from fresh reads, and even an allocate whose response was
lost left a tagged reservation the next planning pass reuses first — so they
catch `ProviderCallError` themselves and fail with the claim **released**.
Holding it would 409 the exact re-run the panel offers as the retry, for the
rest of the 90-minute claim TTL. Enable and disable still serialize against
each other on one fixed claim (`stable_ips:fleet`).

### Cost, quota, and honest copy

AWS bills every public IPv4 identically, attached or not. So **enable is net
~zero per month** (an attached Elastic IP replaces the node's identical
auto-assigned-IPv4 charge) and **disable is what adds cost**: each kept
reservation bills (~$3.60/month, `EIP_MONTHLY_USD` — keep in agreement with
provision's `COST_TABLE["eip"]`) beside the node's new auto-assigned address.
There is no pre-flight quota read — servicequotas would be a new API and IAM
grant for a number AWS enforces anyway; `AddressLimitExceeded` maps to
`address_quota` naming how many nodes were addressed and how many remain
(default quota: 5 public IPv4s per region).

### Hardening details (from the security review)

- The report's policy read carries its own availability flag
  (`egress.policy_available`): an unreadable policy renders "Unknown", never a
  canonical "off", and blocks both actions (`policy_unavailable`).
- A fleet action's typed echo is ALWAYS the action word — a caller-supplied
  `resource` is ignored outright, so neither the echo nor the audit subject
  (`fleet:<operation id>`) can be steered by the request body.
- An explicitly assigned reservation that stopped being free by job time is
  dropped, never tagged — tags are ownership, and stamping them on what may
  now be somebody else's address would make every later reuse pass wrong.
- A disable whose fresh serving read comes back EMPTY while addresses were
  attached at request time fails `address_unverified` instead of reporting a
  verified-sounding "off".
- A dispatch failure on these two actions says the truth: the policy IS
  recorded, only the convergence did not start.

### Balancer-less installs: read-only fallback

Fleet discovery is the serving map, so an install with no load balancer has no
fleet here and the toggle is deliberately absent (`no_fleet_nodes`). But the
report still answers the question the operator came with: when the serving
read succeeds and finds nothing registered, `egress.fallback_attached` lists
every Elastic IP attached to an EC2 instance in the region (instance
association is the filter — balancer- and NAT-held addresses are inbound
plumbing and excluded), with the instance name resolved through the same
describe. `_fallback_attached` fills a separate key on purpose: the offers and
both runners read only the fleet-scoped facts, so a fallback row can never
make a mutation look available. Single-node provisioned or tofu-era estates
therefore show their vendor-allowlist address with no control attached.

### Boundaries

- The fleet is what `serving_map()` shows: **registered** instances. A fleet
  with no balancer is invisible here — provision already gives those nodes
  their addresses (the node is the DNS target). A node whose drain completed
  is deregistered and will not be converged by a later enable; drain itself
  never detaches an address, so enable-then-drain keeps the node allowlisted.
- Provision-side, `spec.stable_node_ips` (env-file key, `--stable-node-ips`)
  keeps `_ensure_addresses` running even behind an NLB — the birth-time form
  of the same policy. The two do not fight: DNS's balancer branch never reads
  node addresses, and an admin disable will be re-attached by the next
  `provision apply`, so change both or expect that.

## RDS readers

The shape is resolved **live**, because Aurora and a standalone instance take
different operations:

| Shape | Add | Why |
|---|---|---|
| Aurora cluster | `create_db_instance(DBClusterIdentifier=…)` | Aurora does **not** support `CreateDBInstanceReadReplica` for a cluster; the cluster replicates and a reader is a plain member |
| Standalone instance | `create_db_instance_read_replica` | It has no cluster |

Removal is `delete_db_instance(SkipFinalSnapshot=True)`, refused unless the
target **provably** is a reader — `IsClusterWriter=False` for an Aurora member,
a non-empty `ReadReplicaSourceDBInstanceIdentifier` for a standalone. Skipping
the final snapshot is only correct because of that proof: a reader is a copy of
data that lives on the primary.

**django-mojo does not consume a reader endpoint today.** `DATABASES` points at
one host and every query goes there. Adding a reader adds standby read capacity
and an endpoint string; nothing in this application gets faster until the
project wires that endpoint into its own configuration. The panel states this,
and the completion message hands over the endpoint.

## ElastiCache replicas

`increase_replica_count` / `decrease_replica_count`, with `ApplyImmediately`
required and stated by the caller. ElastiCache offers no maintenance window for
a replica-count change; rather than silently rewriting a `false` into a `true`,
the apply refuses and says what the operator is agreeing to.

A decrease **never sends `ReplicasToRemove`**. Naming the node to kill means
choosing which availability zone loses its standby, and ElastiCache picks
better than a portal that cannot see the shard layout.

Two refusals, both before the API call:

- **Cluster-mode enabled** — refused by name. Its replica count is per shard
  and changing it is a resharding decision, not a capacity change.
- **The failover floor** — `AutomaticFailover` or `MultiAZ` enabled means the
  minimum is **one** replica, not zero. Zero is offered only with failover off,
  and the confirmation states the loss: nothing to fail over to.

**A replica in a replication group is failover capacity, not read throughput.**
django-mojo talks to the primary endpoint only.

## Resizing (curated sizes)

`resize_cache` (ModifyReplicationGroup `CacheNodeType`) and `resize_database`
(ModifyDBInstance `DBInstanceClass`), on the same operation contract as every
other capacity action: fresh guards at apply time, single-flight claim,
phased job with a bounded settle loop, audit event, plain-English completion
note.

### The ladder lives in provision's `spec.py`

`CACHE_SIZES` and `DB_SIZES` sit in `mojo/deploy/provision/spec.py` beside
`COST_TABLE` — one file answers "what sizes exist and what do they cost", the
existing priced-preset test pattern extends to guard them
(`tests/test_deploy/provision_spec.py`), and the import is free (spec imports
only `re`). The report's `sizes` block is exactly this data with the price
per rung, so the panel renders the allowlist rather than hardcoding it.

**The API takes a size KEY (`small`/`medium`/`large`/`xlarge`), never an
instance type.** `_resolve_size` refuses anything off the ladder before any
provider call, so a client cannot smuggle an arbitrary type and the ladder
can be re-pointed at new families without breaking callers.

### The database resize is PER INSTANCE

The writer and the readers carry independent sizes — readers deliberately
smaller, because that is where the money is. The action targets one named
instance (an Aurora writer or reader via `instance_role` +
`cluster_members`'s `IsClusterWriter`, or a standalone primary or replica),
and "resize the readers" is therefore N applies, one per reader — the Fleet
page stages one reader size and submits one batch step per differing reader,
which the plan/apply batch runs as ordinary sequential applies.

**PromotionTier rides in the SAME ModifyDBInstance call**: writer 0, Aurora
reader 1, standalone never (the parameter is not sent off-Aurora). Aurora
therefore prefers the writer-class box in any failover where it is available,
and the "tier call fails after a successful resize" failure mode is designed
out rather than handled. Nothing here ever moves the writer role — no
failover, no promote; the overnight failback after a failover is its own
item.

### Interruption semantics, stated before apply

- **Cache**: node type is group-wide (replicas always match their primary).
  With automatic failover and ≥1 replica the change is a rolling replace —
  replicas first, then a brief failover — one short interruption. With no
  replica the primary is down for the duration. Each cache row carries
  `resize_impact` (`"rolling"`/`"downtime"`) so the case is on screen before
  the button.
- **Database**: `apply_immediately` must be literally `true` for both actions
  (present, boolean, true — mirroring `set_cache_replicas`): a
  maintenance-window deferral has no bounded settle and would make an
  observed operation report a timeout for a change that is merely queued. A
  writer resize restarts the instance — minutes offline, and the completion
  note owns it; a reader resize pauses only that reader.
- **Downsizing is allowed** — the guard is identity (`no_change`), not
  direction — and a cache downsize's completion note states the eviction risk
  under maxmemory honestly, without pretending to measure a working set the
  report does not carry.
- **`not_settled`**: a resize against a resource that is not settled is
  refused with the provider state quoted verbatim, never blamed on an
  in-flight change. Benign background states pass: cache `snapshotting`; RDS
  `backing-up`, `storage-optimization`, `maintenance`. `deleting` stays
  refused — that closes the race with `remove_reader`.

### Claims

`resize_cache` **shares set_cache_replicas' per-group claim key** (the
deployed literal, not a rename): a resize and a replica-count change mutate
the same AWS object and must serialize, and reusing the literal keeps a claim
held across a deploy honored. `resize_database` takes its own per-instance
key, so two readers resize concurrently while one instance never carries two
changes.

### Report enrichment

`_cache_rows` gains `node_type` (group-wide) and `resize_impact`;
`_database_rows` gains `writer_instance_class` and `reader_instance_classes`
for Aurora (from the one `instance_statuses` describe — `rds._instance_facts`
now carries `instance_class`, so no extra API call) and folds each standalone
replica's class into its source's `reader_instance_classes`. These are the
exact field names the Fleet page already consumes, so its type tags and size
selects light up with no negotiation.

## Cross-cutting mechanics

**Single flight.** `add_node` claims one FIXED literal key
(`…:claim:add_node:fleet`) so two adds serialize even when they name different
resources — they would otherwise race on the image, the topology write and the
convergence. Everything else claims `action:resource`. A cache that cannot
answer is a **503**, never a go-ahead; `cache.add` returning `False` with
nothing behind it is the same outage, not a holder.

**Operation records live in the cache**, keyed by uuid, with a 90-minute TTL
that matches the claim. An operation is a bounded observation of AWS state, and
losing the observation loses nothing `report()` cannot re-derive from the
provider; every mutation is AWS-side before its record is written. `GET
/api/aws/capacity/status` is a pure read — a status endpoint that ADVANCED the
work would let a `manage_aws`-only caller drive a registration.

**Cache invalidation.** Every mutation drops the capacity report key and
`admin_platform.LOAD_BALANCER_CACHE_KEY`, so the Dashboard's serving-tier rows
do not keep a 60-second lie about fleet membership.

**Permissions.** `manage_aws` for the reads; `manage_aws` **AND a literal
superuser** for the apply, plus `denies_key_backed_session` and
`requires_fresh_auth(600)`. `manage_platform` is the grant that redeploys the
fleet — it is not the grant that terminates it.

**Audit.** Every apply writes one `admin_platform.audit_after_commit` event
whose action comes from a fixed map, so the trail cannot be steered by a
request body.

## Batch plan/apply

The Fleet Scaling page's one-shot form of the same actions: `plan_batch`
validates an ordered set of steps and stores a short-lived plan;
`apply_batch` confirms it by id and hands the steps to ONE job
(`asyncjobs.capacity_batch` → `run_batch`) that executes them sequentially
through the **unchanged** `apply()`. Not a desired-state reconciler: guards
stay per-action and are never re-implemented against a hypothetical future
state.

### The plan store

Plans are cache records (`…:plan:<uuid>`, `PLAN_TTL = 300`), same idiom as
operation records — a plan is a bounded observation of AWS state, worth
exactly as much as one, and a table would outlive its own validity. A plan
that cannot be stored answers `cache_unavailable` 503 (a plan that cannot be
stored cannot be confirmed — the `_claim` stance). At most `MAX_BATCH_STEPS`
(20) steps; the stable-IPs pair is refused from batches outright (fleet-wide,
its own fixed claim, runs alone).

Plans validate against the **cached** `report()` (≤`REPORT_TTL` old) because
the page re-plans on every debounced stepper tweak — a full describe sweep
per keystroke would rate-limit the account for an answer the cache already
holds. Only the apply takes `report(refresh=True)`. Both refuse
`report_degraded` 503 when the envelope's `warnings` name a section any step
touches (`_BATCH_SECTIONS`) — a throttled describe must never surface as
"unknown resource", and a degraded fresh envelope must never be
fingerprinted.

### The fingerprint

`_fleet_fingerprint` is a sha256 over a canonical projection of the
STRUCTURAL facts a plan's safety depends on: mode; node `(id, healthy,
instance_type)` tuples; database `(identifier, kind, writer, readers,
instance_class, writer_instance_class, reader_instance_classes)` —
`instance_class` included so a standalone writer's resize changes the hash,
not just Aurora's; cache `(identifier, replica_count, node_type,
cluster_enabled)`. Transient `status` strings are deliberately excluded so a
`backing-up` flap does not 409 a valid plan. Server-side only — never
returned to the client, so there is no forgeable surface. Apply recomputes
it from the fresh sweep and refuses `plan_stale` 409 on mismatch, never
silently re-planning.

### Ordering

`_order_steps` stable-sorts by rank — `add_node` 0, `add_reader` 1,
`set_cache_replicas` grow 2, `resize_cache` 3, `resize_database` 4,
`set_cache_replicas` shrink 5, `remove_reader` 6, `drain_node` 7,
`terminate_node` 8 — then pins each terminate immediately behind the drain
naming the same resource. Capacity never drops before a disruptive change,
and the returned plan's `order_note` says so.

### Plan-time validation

The offers gate (`envelope["actions"][a].offered`) plus report-level
resource/param checks (mirroring `_apply_cache`'s static checks, the curated
size table, ≠-current) plus cross-batch rules: the healthy-drain budget
(only drains of currently-healthy nodes count, and at least one healthy node
must remain — parity with the page's `removable()`), terminate-needs-a-drain
(earlier in the batch, or a node whose drain already completed),
`conflicting_steps` for a batch that both resizes and removes a resource.
Every refusal names the step index in `data.step`. Full provider
re-derivation stays at execution time.

### Costs

`_step_cost` prices each step from provision's `COST_TABLE`
(adds/removes ± the type's price, replica deltas × node type, resizes
new−old — × member count for cache, whose node type is group-wide). Any type
missing from the table → `monthly_delta_usd: None` + a step warning +
`estimate_complete: false` on the plan; the total sums only priced steps.
Never a silent $0.

### The runner

`apply_batch` consumes the plan **atomically** — `cache.add` on
`…:planlock:<plan_id>` either claims it for this batch or answers
`plan_already_applied` 409 carrying the batch id already running, so a
double-click converges on polling. It writes the batch record
(`…:batch:<uuid>`, TTL = summed step deadlines + margins + `CLAIM_TTL`) and
publishes `capacity_batch` on the cleanup channel with `max_retries=0` (a
redelivered batch would re-run mutations); `max_exec_seconds` is stored
operator metadata, not a kill switch. A publish failure marks the batch
`batch_dispatch_failed` and answers 503 — nothing was started.

`run_batch` walks the steps: each is `apply(actor, action, resource,
**params)` — the real claims, the real fresh-read guards, a real child
operation and its own job, identically to a manual click (the job engine is
a thread pool, so orchestrator and child coexist). It writes the per-step
audit row the REST layer would have (`AUDIT_ACTIONS[action]`,
`resource:op_id`) — the batch-level `aws_capacity_batch` row is written by
the REST handler at apply. Then it polls the child record on
`POLL_INTERVAL`, mirroring `phase`/`message` onto the step (one batch poll
answers everything, and `updated` stays fresh), under a **backstop ceiling**
of `_deadline_for(action) + 900` (+ `canary_timeout()` for `add_node`,
whose `_deadline_for` under-budgets the proving leg). The ceiling only
catches a wedged or vanished child — the child's own settle timeouts are
the clock, because a ceiling that undercuts a legitimately slow child
converts a succeeding mutation into a reported failure.

First failure — a guard refusal, a claim conflict (`capacity_in_progress`
from a concurrent single apply; per-step claims arbitrate both directions),
a failed/vanished/over-ceiling child (`operation_vanished` when the child's
record disappears from the cache mid-poll, `operation_timeout` when it is
still running past the backstop ceiling) — fails that step with the child's
code, marks every later step `not_attempted`, fails the batch with "Step N
of M failed: …; the remaining K step(s) were not attempted.", and stops.
**No rollback.** No claims of its own to release — each child releases its
own via `_finish`/`_fail`.

### What survives a restart

Plan, batch and operation records live in the shared cache and survive
process restarts — but on a single-node install the batch runner and its
in-flight child share the engine's pool, so both threads die together.
`max_retries=0` means the reaper marks the Job rows failed with no
redelivery: remaining steps never start (fail-safe), and the batch record
stays `running`-stale until its TTL. `batch_status` surfaces that honestly
with `stalled: true` (no write in ~3 minutes); nothing auto-resumes. Held
per-step claims expire on `CLAIM_TTL`. The engine's graceful stop waits for
running threads, so **avoid deploys mid-batch** — a multi-hour batch
obstructs `jobman restart` the same way a long `add_node` already does. A
cleared cache loses everything, the same honest answer as
`operation_status`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `ADMIN_CAPACITY_IMAGE_MAX_AGE_DAYS` | `14` | Maximum age of a `mojo:fleet-image` AMI that may be reused instead of captured |
| `ADMIN_CAPACITY_NODE_ROOT` | `/opt/api` | `--root` passed to `node_setup` in the clone's user-data |
| `PRIMARY_BALANCER_HOST` | unset | Read (not written) to prefer a non-primary clone source and to label the row |
| `EDGE_NODE_ID` | unset | **If set, adding a node is refused** (`node_id_pinned`) — every node would report the same readiness identity, so a new node could never be proven |
| `AWS_STABLE_OUTBOUND_IPS` | unset | **Protected system setting, not django.conf** — the durable stable-egress policy, `{"enabled": bool}`, written only through the capacity apply |
| `INFRASTRUCTURE_MODE` | `managed` | `external` refuses every apply |

## IAM

Beyond what the dashboard already needs:

```
ec2:DescribeInstances       ec2:CreateImage        ec2:DescribeImages
ec2:RunInstances            ec2:TerminateInstances
ec2:CreateTags              iam:PassRole            (for the cloned instance profile)
ec2:DescribeAddresses       ec2:AllocateAddress
ec2:AssociateAddress        ec2:DisassociateAddress (stable outbound IPs)
elasticloadbalancing:DescribeLoadBalancers
elasticloadbalancing:DescribeTargetGroups
elasticloadbalancing:DescribeTargetHealth
elasticloadbalancing:DescribeTargetGroupAttributes
elasticloadbalancing:RegisterTargets
elasticloadbalancing:DeregisterTargets
rds:DescribeDBInstances     rds:DescribeDBClusters
rds:CreateDBInstance        rds:CreateDBInstanceReadReplica
rds:DeleteDBInstance        rds:ModifyDBInstance        (resize_database)
elasticache:DescribeReplicationGroups
elasticache:IncreaseReplicaCount
elasticache:DecreaseReplicaCount
elasticache:ModifyReplicationGroup                      (resize_cache)
```

A denial surfaces as `provider_denied` carrying **only** the IAM action —
`ProviderCallError.detail()` is the only shape that reaches an API response, a
log line, or the database.

## Operator tasks this does NOT do

- **AMI cleanup.** Captured `mojo:fleet-image` AMIs are reused for 14 days and
  then simply stop being reused. They are not deregistered and their snapshots
  are not deleted, and terminating a node does not reap the image it came from.
  Deleting old fleet images (and their snapshots) is an operator task. Reaping
  automatically would mean deleting the one artifact a rollback might want,
  from a background job, with no confirmation.
- **Scaling policies.** Every action here is one deliberate, confirmed,
  audited change. Nothing autoscales.
- **Bootstrapping.** The first node of an installation is the project's
  provisioning, not this button.
- **The zero-downtime Aurora writer resize.** The recipe — resize a reader,
  fail over to it, resize the old writer — is still not one button. The
  batch API composes capacity steps, but failover is not a capacity action:
  the resize actions never move the writer role (no failover, no promote —
  only instance classes and their promotion tiers), so the middle of that
  recipe stays a deliberate console move.
