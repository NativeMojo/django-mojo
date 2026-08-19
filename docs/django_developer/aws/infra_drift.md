# Fleet drift — what is serving traffic vs. what the portal recorded

`mojo/apps/aws/services/infra_drift.py` is a read-only daily scan that answers
one question no dashboard answers: **did anything change outside this portal?**

It compares the EC2 instances actually registered behind the load balancers
with the node ids recorded in the protected `EDGE_EXPECTED_TOPOLOGY` setting,
in both directions, and files at most **one** incident event per run. It creates
nothing, changes nothing in AWS, and every remediation it prints is something a
person does.

Off by default. Arm it with `AWS_INFRA_DRIFT_ENABLED = True` (file-only) and
install its RuleSet with `manage.py aws-check --apply --section rules`.

## What is compared

| Side | Source |
|------|--------|
| What is serving | `elbv2_helper.serving_map()` — every attached target group of every balancer, described fresh, with each group's registered targets |
| What was recorded | `system_settings.get_value(EXPECTED_EDGE_TOPOLOGY)["nodes"]` — the fleet the operator declared in System Setup |
| Instance identity | `ec2_helper.instance_map()` — `private_hostname`, `private_dns_name`, the `Name` tag, `state` and tags for each registered instance |

The AWS helpers are read directly rather than through `capacity.report()`
because `capacity._node_rows` does not carry `private_hostname`, which is the
single most reliable identity here.

**With no recorded intent there is no drift.** An empty or absent
`EDGE_EXPECTED_TOPOLOGY` returns `status="unconfigured"` and files nothing —
otherwise every node in the fleet would be a level-5 finding, daily, forever.

## Matching: four normalized candidates

Both sides go through **one** function:

```python
def _norm(value):
    return str(value or "").strip().lower().replace(".", "-").replace("_", "-")
```

This is load-bearing, not tidiness. The two sides disagree about the domain:

- A topology node id is what `jobs.job_engine.host_channel()` produces —
  `socket.gethostname()` lowercased with `.` and `_` turned into `-`, **domain
  kept**. A box whose `gethostname()` returns `ip-10-0-1-23.ec2.internal` is
  recorded as `ip-10-0-1-23-ec2-internal`.
- `facts["private_hostname"]` is `PrivateDnsName.split(".", 1)[0]` — **domain
  stripped**, i.e. `ip-10-0-1-23`.

Compared raw they never match, and the scan false-positives every day on
exactly the hand-built nodes `private_hostname` exists to identify.

A registered instance is considered recorded when **any** of these normalized
candidates equals a normalized recorded node id:

1. `private_hostname` — `ip-10-0-1-23`
2. `private_dns_name` — `ip-10-0-1-23.ec2.internal` → `ip-10-0-1-23-ec2-internal`
3. the `Name` tag
4. the **suffix identity** — any recorded node whose final `-`-delimited label
   is exactly the instance id's final label. `i-0abc123def456` matches a
   recorded `mojo-node-0abc123def456`.

Candidate 4 exists because `capacity.expected_node_id(base_name, instance_id)`
builds a node id as `<base>-<instance-id suffix>` and the base is a per-fleet
value this scanner never sees. The comparison is **exact equality of the final
label**, not `endswith` — `api-x0abc` must not satisfy a suffix of `0abc`.

**The raw `Name` tag is not a candidate on its own** — only normalized, and only
alongside the other three. An EC2 `Name` is free text an operator can set to
anything, is not unique, and is not what the box reports as its hostname; making
it the identity would mean a node renamed in the console silently becomes a
different node, and two nodes sharing a `Name` collapse into one.

## Both directions

**Forward — `unrecorded_node` / `capacity_added_not_recorded`.** An instance
that is registered in a target group and is answering requests, that no recorded
node matches. If it carries `mojo:created-by=admin-capacity` the reason is
`capacity_added_not_recorded`: the portal itself added the node and the topology
record was not updated, which is a different sentence to the operator than "a
box appeared".

**Reverse — `node_unserving`.** A recorded node that no registered instance
matched. Note the asymmetry that makes this direction safe: it is computed from
**recorded** names, so an opaque hand-built node id needs no identity derivation
at all. Only a node the topology already names can produce this finding.

## Event category vs. RuleSet category

Same split as [version drift](version_drift.md), for the same two reasons:

| Constant | Value | Role |
|----------|-------|------|
| `CATEGORY` | `system:health:infra_drift` | The **event** category — puts the row on `/api/incident/health/summary` |
| `RULESET_CATEGORY` | `infra:drift` | The **RuleSet** category, matched through the event's `scope` |

The RuleSet category stays **outside** `system:health:` deliberately. The
incident cronjob's health-defaults bootstrap guard is a prefix match, and a
RuleSet inside that namespace once made it permanently satisfied — `Health -
Runner Down`, `Scheduler Missing` and `TCP Overload` were never installed at all.

`RuleSet.ensure_infra_drift_rules()` installs `Health - Infrastructure Drift`
(priority 5, `MatchBy.ALL`, `BundleBy.NONE`, one rule `level >= 5`) with handler
`notify://perm@manage_security` — **notify only, no `ticket://`**. Drift is a
one-minute reconciliation in System Setup, and on an externally-managed estate it
recurs until someone records the node; a ticket per run would be a board full of
the same two lines. It is never added to `ensure_default_rules()`.

## Levels

| Level | Meaning | Filed? |
|-------|---------|--------|
| 1 | The serving fleet matches the recorded fleet | **No event at all** |
| 4 | An AWS read did not answer (denied, or a provider error), or a result was truncated | Yes — health-strip row only, below the RuleSet's `level >= 5` gate |
| 5 | Drift found, in either direction | Yes — notifies `manage_security` |

Never above 5. **Level 1 files nothing**, and that is a correctness requirement,
not a preference: `Event.publish` creates an Incident whenever any RuleSet
matched, and the catch-all RuleSet matches `level >= 1` through its `"*"`
fallback with no handler. An "all clear" event would manufacture a permanent,
handler-less Incident on every single run.

`status` is separate from `level`:

| `status` | When | Filed? |
|----------|------|--------|
| `ok` | The comparison ran | Depends on level |
| `unconfigured` | `EDGE_EXPECTED_TOPOLOGY` records no nodes | No |
| `unavailable` | `provider_code` was `credentials_unavailable` or `network_unavailable` | No |

`unavailable` is branched on the **provider code**, never on the exception type.
`serving_map` and `instance_map` go through `ProviderCaller.call`, which catches
`Exception` and re-raises `ProviderCallError` — an `except NoCredentialsError`
here would never fire, and every credential-less box (every dev machine, every
test run) would file a level-4 event daily. For the same reason,
`ProviderCallError.detail()` carries `iam_action` only when `denied` is True, so
the action is read off `err.iam_action` or the caller-known literal.

## Truncation is a warning, never a finding

Three caps live upstream and cut before any counter here sees the rows:

| Cap | Where |
|-----|-------|
| 20 target groups | `elbv2.serving_map(max_groups=20)` |
| 100 targets per group | `elbv2.target_health` (`MAX_TARGETS`) |
| 100 instance ids per describe | `ec2.instance_map` (`ids[:100]`) |

Hitting one emits a `groups_truncated` / `instance_truncated` warning naming what
was not compared. A row nobody read must never become drift.

If `instance_map` fails outright, the scan reports the warning and **no
findings**: without instance facts the only usable identity is the suffix, and
every hostname-recorded node would read as drift.

## The external reframe

`INFRASTRUCTURE_MODE=external` means an IaC pipeline owns the estate and the
portal only observes. That changes **only the `What to do:` sentence** in each
finding — same level, same finding count, same rows. An externally-managed
installation is not less drifted; it is drifted for a reason the operator already
knows, and the action is to *record* the node rather than to remove it.

Telling an external installation to deregister a node its pipeline owns would be
the wrong instruction, so that sentence never appears in external mode.

## The output is prose, on purpose

The event is read by one operator on the health strip with no other context, so
each finding says what it is, who it costs something, and what a person should
do — and says plainly that nothing here changed AWS:

```
- i-0abc123def456 ("web-04", ip-10-0-3-17) is registered in target group
  prod-api-tg and is answering requests, but no node in EDGE_EXPECTED_TOPOLOGY
  matches it.
  Who is affected: production traffic is being served by a box the portal does
  not track, so fleet readiness, deploys and pool convergence all skip it — a
  broken release on this node will not show up in System Setup.
  What to do: if this node belongs to the fleet, add "ip-10-0-3-17" to System
  Setup > Expected fleet. If it does not, deregister it from target group
  prod-api-tg. Nothing here changes AWS for you.
```

The structured rows are in the event metadata (`findings`) for a UI that wants
them: `instance_id`, `name`, `private_hostname`, `instance_state`,
`target_groups`, `added_by_capacity`, `reason`, `suggested_node_id`, `note`,
`remediation`.

`suggested_node_id` is a **hint**, and the name says so. It is the `Name` tag
plus the instance-id suffix — the same shape `capacity.expected_node_id`
produces, with an unverified base, because the real `base_name` is a per-fleet
value this scanner never sees. The authoritative id for a stock EC2 box is its
`private_hostname`, which is what the remediation sentence quotes.

## What this deliberately does NOT do

- **No account-wide sweep.** Only instances registered behind a balancer are
  examined. An idle instance in the account is not serving traffic and is not
  this signal's business.
- **No security-group, IAM, or network posture.** That is
  `mojo/deploy/check_setup.py`'s job, and it judges against a reference
  topology rather than against recorded intent.
- **No RDS reader / cache replica counts.** Recorded intent for those does not
  exist in `EDGE_EXPECTED_TOPOLOGY`; there is nothing to compare against.
- **No System Setup readiness section.** `system_setup` only calls a fix
  operation successful when `overall == "pass"`. This signal warns on a
  *correct* installation by design (an unrecorded node in an external estate is
  expected), so a readiness section would permanently break the portal's
  unscoped "Fix all".
- **No `fix`.** Nothing here writes to AWS or to `EDGE_EXPECTED_TOPOLOGY`.
  Recording a node is a protected-setting write that belongs to a person.

### Deferred, and what would unblock each

| Deferred | Unblocked by |
|----------|--------------|
| Reader/replica drift | A recorded intent for the data tier — an `EDGE_EXPECTED_TOPOLOGY` sibling that names expected readers and replicas |
| An account-wide unknown-instance sweep | A reliable ownership signal on every instance. `mojo:project` / `mojo:env` / `mojo:role` are **not** stamped by any shipped code today; only `mojo:created-by`, `mojo:fleet-image` and `managed-by=django-mojo` are |
| Auto-recording a capacity-added node | Nothing technical — it is a policy call. `capacity` already knows the node id it derived; writing it into the topology at add time would remove the `capacity_added_not_recorded` reason entirely |
| A readiness section | A `system_setup` fix contract that distinguishes "advisory, may stay warn" from "must go green" |

## Settings

| Setting | Kind | Default | Meaning |
|---------|------|---------|---------|
| `AWS_INFRA_DRIFT_ENABLED` | file-only, bool | `False` | Arms the daily scan (`mojo.apps.aws.cronjobs.check_infra_drift`, 07:20). Read **inside** the cron function, so it is not frozen at import. |

## Wiring

| Piece | Where |
|-------|-------|
| Scanner | `mojo/apps/aws/services/infra_drift.py` (`InfraDriftScanner`, `scan()`) |
| Cron | `mojo/apps/aws/cronjobs.py` — `@schedule(minutes="20", hours="7")`, offset from the version-drift scan |
| Job | `mojo/apps/aws/asyncjobs.py` — `check_infra_drift`, plus `_infra_title` / `_infra_details` |
| RuleSet | `RuleSet.ensure_infra_drift_rules()`; `aws-check --apply --section rules` installs it |
| Tests | `tests/test_aws/infra_drift.py` |

The cron body is a settings read plus `jobs.publish` and nothing else, on
purpose: `run_scheduled_functions` wraps the whole loop in one `try` with a
re-raise, so anything that raises in a cron body aborts every other scheduled
function on that tick, fleet-wide. The AWS calls belong in the job, where a
failure costs one job.

## Related

- [version_drift.md](version_drift.md) — the same event/RuleSet split, for managed-service versions
- [capacity.md](capacity.md) — where `mojo:created-by=admin-capacity` and the derived node id come from
- [infrastructure_mode.md](infrastructure_mode.md) — the managed/external switch
- [aws_check.md](aws_check.md) — the `rules` section that installs the RuleSet
