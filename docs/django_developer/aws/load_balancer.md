# Load balancer (ELBv2)

`mojo.helpers.aws.elbv2` has **two client paths**, deliberately separate:

- **`LoadBalancerHelper`** — the read seam behind the Admin dashboard. Which
  balancer fronts the fleet, which target groups are attached to it, and how
  many registered targets are healthy. It exists so the dashboard can answer
  "can customers reach us right now" from the balancer's own health checks
  rather than from a raw instance count. **It mutates nothing.**
- **The module-level functions** — the serving-tier MUTATIONS used by the
  [capacity actions](capacity.md), plus the two reads their guards need. See
  [Module-level mutations](#module-level-mutations) below.

Every call in both paths goes through the safe provider boundary, so a denial
or a network failure raises `ProviderCallError` and raw botocore text — which
can carry credentials, signed URLs, and request parameters — never reaches a
response, a log line, or the database.

```python
from mojo.helpers.aws.elbv2 import LoadBalancerHelper

helper = LoadBalancerHelper()          # AWS_KEY / AWS_SECRET / AWS_REGION
frontend = helper.frontend()
names = helper.instance_names(frontend["instance_ids"])
```

## `frontend(max_groups=4)`

One bounded read of the public serving tier. Returns:

| Key | Meaning |
|---|---|
| `configured` | A load balancer exists and was read |
| `balancer` | `name`, `arn`, `type`, `scheme`, `state`, `dns_name`, `zones`, `addresses`, `elastic_ips` |
| `groups` | Attached target groups: `name`, `target_type`, `protocol`, `port`, `registered`, `healthy`, `targets`, `observed` |
| `registered` / `healthy` | Totals across the attached groups |
| `instance_ids` / `healthy_instance_ids` | Registered instance targets, for scoping an EC2 row |
| `denied` | IAM actions that were refused, deduped |
| `failures` | Wire-safe `ProviderCallError.detail()` for every failed call |
| `truncated` | More attached groups than `max_groups` |

Three deliberate shapes:

- **`describe_target_groups` is called ONCE for the whole account**, and
  attachment is read off each group's `LoadBalancerArns`. This is the same
  pattern `aws_check` and `check_setup` use. A per-balancer loop would be an
  N+1 against a rate-limited API for an identical answer.
- **Elastic IPs come from the balancer itself** —
  `AvailabilityZones[].LoadBalancerAddresses[].AllocationId`. There is no
  `describe_addresses` call: an allocation id is the only proof the address
  survives the balancer being replaced, and it is already in the response.
- **Failures are collected, not raised.** A denied `DescribeTargetHealth` still
  returns the balancer facts already read, with
  `elasticloadbalancing:DescribeTargetHealth` in `denied` so the caller can
  tell an operator exactly which grant to add.

Clients are built with `timeout=2, max_attempts=1`. A dashboard read has a
response budget of seconds; retrying a denial or a timeout three times burns it
for the same answer.

## `instance_names(instance_ids)`

Name tags for at most one page of instances, in a single `describe_instances`.
Ids that do not look like instance ids are dropped, an untagged instance falls
back to its own id, and an empty list makes no call at all. Unlike `frontend()`
this raises `ProviderCallError` — the caller decides whether a missing name is
worth degrading a row for.

## IAM

Grant the read actions to whichever identity the platform uses:

```
elasticloadbalancing:DescribeLoadBalancers
elasticloadbalancing:DescribeTargetGroups
elasticloadbalancing:DescribeTargetHealth
ec2:DescribeInstances
```

The module-level mutations need three more:

```
elasticloadbalancing:DescribeTargetGroupAttributes
elasticloadbalancing:RegisterTargets
elasticloadbalancing:DeregisterTargets
```

`botocore` names the client `elbv2`, but IAM names the service
`elasticloadbalancing`; the helper passes that explicitly so a denial reports
the action an operator can actually paste into a policy.

## Degradation

| Situation | Result |
|---|---|
| No credentials | `configured: False`, a `credentials_unavailable` entry in `failures` |
| No load balancer | `configured: False`, nothing in `denied` |
| A call denied | Facts already read are kept; the IAM action lands in `denied` |
| A group attached to nothing | Skipped — it publishes no health |
| More than `max_groups` attached | Bounded, with `truncated: True` |

`account.services.admin_platform._load_balancer()` turns this into the
dashboard's status ladder: `unhealthy` only when a group with registered
targets has zero healthy (or a serving group sits behind a balancer with no
address at all), `degraded` when some but not all are healthy, `unconfigured`
when nothing is registered, and `unknown` when a call was denied. See
[the Dashboard contract](../account/admin_portal/dashboard.md).

## Module-level mutations

Used by `mojo/apps/aws/services/capacity.py`. Not methods on
`LoadBalancerHelper`: that class is a cached, 2-second-timeout dashboard reader,
and a registration is neither cacheable nor allowed to time out at two seconds.

| Function | Does |
|---|---|
| `register_target(group_arn, instance_id, port=None, ...)` | Puts one instance into one target group — the LAST step of an add |
| `deregister_target(group_arn, instance_id, port=None, ...)` | Starts draining one instance out of one group. Returning means AWS accepted the request, **not** that the drain finished |
| `target_health(group_arn, instance_id=None, ...)` | `[{id, port, state, reason}]`, freshly read |
| `target_group_attributes(group_arn, ...)` / `deregistration_delay(group_arn, ...)` | The group's configured drain window |
| `drained(rows)` | Whether nothing in `rows` is still carrying traffic |
| `serving_map(client=None, region=None, max_groups=20)` | EVERY attached group of EVERY balancer, with each group's targets |

Four things these get right that the obvious version does not:

**`mutation=` is explicit.** `ProviderClient` derives "is this a mutation?"
from the method-name prefix, and neither `register_` nor `deregister_` is one
of its prefixes. Left to inference, a failed `DeregisterTargets` would report
`mutation_state="none"` — "AWS did not start draining your node" — when AWS may
well have. That is the one question that matters before deciding to retry.

**`target_health` never filters provider-side.** AWS raises `InvalidTarget` for
a target that is not registered, which is exactly the answer a drain poll is
waiting for. One unfiltered describe, narrowed in process, lets "it is gone" be
a value instead of an exception.

**`drained()` treats `draining` and `unhealthy.draining` as in-flight.** AWS
reports them for the whole deregistration delay. Only `unused` — or absence
from the group — is drained. Treating either in-flight state as done is how a
node gets terminated with live connections on it.

**`serving_map` is deliberately uncached and multi-balancer.** `frontend()`
reads the FIRST balancer and caches for a minute: right for a dashboard row,
wrong for a guard. A node that is the last healthy target of an internal group
behind a second balancer is still the thing keeping that group up.
