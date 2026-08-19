# Infrastructure mode

One switch that says whether this installation's AWS estate is the portal's to
mutate. On an installation whose infrastructure is declared and applied by an
external IaC pipeline, a portal mutation is not merely unwanted — it is a change
the next apply will revert, or replace with something different.

```python
# settings.py
INFRASTRUCTURE_MODE = "external"
```

## The two values

| Value | Meaning |
|---|---|
| `managed` (default) | This portal owns the infrastructure. Unset and `""` mean this — every existing installation keeps working with no change. |
| `external` | Something else owns it. The gated mutations answer 403 `infrastructure_external`. |

**Anything else is treated as `external`**, with one logged error naming the
setting and the rejected value. Same reasoning as
`AUTH_HANDOFF_GROUP_TOKEN_MODE`: a typo in a switch whose entire job is to
refuse must not silently turn the refusal off. A settings read that *raises* is
treated the same way — a broken read is not a licence to mutate.

## File-only, deliberately

The mode is read with `settings.get_static`, so it comes from the settings file
and nowhere else. A DB/Redis-backed `Setting` row is writable through the
generic `/api/settings` REST plane, and a remotely-writable mode would let
settings-write access silently re-arm every mutation this switch exists to
disable. Setting the file value is a deploy, which is exactly the ceremony this
decision deserves.

## The helper contract

`mojo/helpers/infrastructure.py`. It lives in `mojo/helpers/` and not in the
`aws` app on purpose: the Admin bootstrap imports it, and an app-level import
would kill the portal on any installation that does not list `mojo.apps.aws` in
`INSTALLED_APPS`.

```python
from mojo.helpers import infrastructure

infrastructure.infrastructure_mode()   # exactly "managed" or "external". Never raises.
infrastructure.is_external()           # bool
infrastructure.refusal_message(action="")   # the one sentence a human reads
infrastructure.refuse(action="")       # None when managed; a 403 JsonResponse when external

infrastructure.SETTING      # "INFRASTRUCTURE_MODE"
infrastructure.MANAGED      # "managed"
infrastructure.EXTERNAL     # "external"
infrastructure.ERROR_CODE   # "infrastructure_external"
```

`refuse()` is the whole pattern for a REST handler. Make it the **first**
statement in the body — before permission tiers, before body parsing — because
the mode is a property of the installation, not of the caller, and no additional
grant changes the answer:

```python
@md.POST("something/that/mutates/aws")
@md.requires_global_perms("manage_aws")
def on_mutate(request):
    denied = infrastructure.refuse("Doing the thing")
    if denied is not None:
        return denied
    ...
```

The 403 body:

```json
{
  "status": false,
  "error": "Doing the thing is disabled: INFRASTRUCTURE_MODE is external, so AWS infrastructure is managed by your infrastructure team, not this portal.",
  "error_code": "infrastructure_external",
  "data": {"mode": "external", "setting": "INFRASTRUCTURE_MODE"}
}
```

## What is gated today

Two endpoints, each at both layers, the provisioning CLI, and one readiness
section that reports the mode without gating anything:

| Surface | Gate | Backstop |
|---|---|---|
| `POST /api/aws/maintenance/apply` | `mojo/apps/aws/rest/maintenance.py` | `maintenance.apply_upgrade` raises `MaintenanceError(..., "infrastructure_external", 403)` |
| `POST /api/account/admin/platform/framework/update` | `mojo/apps/account/rest/admin_platform.py` | `admin_platform.apply_framework_update` raises `PermissionDeniedException` |
| `python3 -m mojo.deploy.provision apply` | `mojo/deploy/provision/__main__.py` refuses with exit `3` | — the CLI *is* the only caller of its own converge |
| System Setup's `aws_infrastructure` section | reported as a `warn` row, not a refusal | `infra_setup.refuse_external()` raises `DefinitiveSetupFailure` |

### The third surface: `aws_infrastructure`

`mojo/apps/aws/services/infra_setup.py` registers a **read-only** System Setup
section at order 34. It is the third gated surface and the only one whose gate
is not a refusal, which makes it worth stating precisely how it differs from the
two 403s above.

| | The two endpoints | `aws_infrastructure` |
|---|---|---|
| Shape | `JsonResponse`, HTTP 403 | a `system_readiness.result` row |
| Helper | `infrastructure.refuse(action)` | `infrastructure.refusal_message(action)` |
| Status | 403 | `warn` — never `fail` |
| `error_code` | `infrastructure_external` | none; a readiness row carries no error code |
| Trigger | a caller tried to mutate | reading the report |

`refuse()` is the wrong tool here and calling it would be a bug: it returns a
`JsonResponse`, which is meaningless inside a readiness check. The section calls
`refusal_message()` and puts the sentence in the row's `explanation`.

**The section registers with `fix=None`, and that is load-bearing.** Registering
any fixer here — even one that politely refused under `external` — would break
the flow it is meant to participate in, because two shipped behaviors compose
badly:

1. `system_setup._build_steps()` adds a step for **every** fixable section on
   **every** "Fix all" run, regardless of that section's current status.
2. `_execute_planned` treats a raised `DefinitiveSetupFailure` as **terminal**:
   `operation.status = "failed"`.

So an always-refusing infrastructure fixer would hard-fail every Fix-all run on
an external-mode install — before the operator ever reached the sections that
*can* be repaired, and before `final_readiness`. Read-only avoids that entirely.

There is a second trap for whoever adds a fixer later: a section registered with
a `fix` and no `reconcile` hangs forever in `_execute_reconcile`, which returns
`{"status": "pending"}` indefinitely. Re-read `_build_steps` and
`_execute_reconcile` before changing this, and add both halves or neither.

`refuse_external(action_label)` exists as the backstop for any apply path this
module ever grows. Note what it can and cannot do:

```python
from mojo.apps.aws.services import infra_setup

infra_setup.refuse_external("Infrastructure repair")
# None when managed; raises DefinitiveSetupFailure when external.
```

**The message it raises never reaches the operator.** `_execute_planned` catches
`DefinitiveSetupFailure` and records only `exception_class` — the human-readable
explanation is discarded by design, because an exception message is not a
trusted string to render. That is precisely why the explanation lives in the
`check` row instead, where an operator actually reads it.

### What the portal observes, and what only the CLI applies

This is the Setup boundary, and getting it wrong in either direction wastes an
afternoon:

| | Portal (`aws_infrastructure`) | CLI (`python3 -m mojo.deploy.provision`) |
|---|---|---|
| Observes the topology | yes — the same `plan.observe` | yes |
| Creates or modifies anything | **never** | `apply` does |
| Needs a shell on the operator's machine | no | yes |
| Needs AWS credentials | read-only ones, from the running install | provisioning ones |

The portal answers *"is what was provisioned still what the declaration says?"*.
It never answers *"make it so"* — nothing in the portal has, or should have, the
credentials to build a VPC.

**Section codes are frozen once shipped.** `register_section` stamps
`definition_version=1` and System Setup persists `section:<code>` step ids in
`SystemSetupOperation.steps`. Renaming `aws_infrastructure` later strands every
persisted operation that referenced it; a rename needs a reconciliation adapter,
not an edit.

### Three things observe an AWS account, and this is not a fourth

`infra_setup.py` owns no opinion about an account. It resolves which environment
this installation is and hands the spec to `mojo.deploy.provision.plan.observe`
— the same observation the CLI runs before an `apply`. The judgement is the
provisioner's; only the rendering is the portal's.

| Module | When | Judges? | Answers |
|---|---|---|---|
| `mojo/deploy/check_setup.py` | pre-Django | yes, and exits non-zero | "is this account set up correctly?" |
| `mojo/apps/aws/services/aws_check.py` | in-Django | yes, and creates missing integration surfaces | "can this deployment talk to AWS?" |
| `mojo/deploy/provision/discover.py` | pre-Django | no | "what is already there?" |

The import direction is the legal one. `mojo/deploy/` may never import Django or
`mojo.helpers.*` (see `mojo/deploy/__init__.py`); a Django-side module importing
`mojo.deploy.provision` is fine, and is exactly what `infra_setup` does. It must
never become the reason someone adds the reverse import to the provisioner.

### Which environment, and why nothing scans an account it was not given

`mojo.apps.aws` is installed on every fresh clone, and `plan.observe()` is dozens
of Describe calls. So the environment is resolved by **discovering**
`<project>/aws/environments/*.json` — and **no client is constructed until it
resolves**:

| What is on disk | Result |
|---|---|
| exactly one `<env>.json` | that environment is observed |
| several, and `MOJO_ENVIRONMENT` names one | that one is observed |
| several, and nothing names one | `pending` — remediation names `MOJO_ENVIRONMENT` |
| none | `pending` — remediation names the provisioning CLI |
| the named `<env>.json` is absent, unreadable, or fails `inputs.problems()` | `pending`, naming the environment |

`MOJO_ENVIRONMENT` is read with `settings.get_static`, so it is file-only for the
same reason `INFRASTRUCTURE_MODE` is. Every unresolved case is `pending`, never
`fail`: an installation whose infrastructure someone else built is not broken.
And every one of them makes zero AWS calls — the failure mode this avoids is a
fresh clone quietly scanning a stranger's account because a section was
registered.

The observation is cached in `django.core.cache` for 120 seconds (matching
`capacity.REPORT_TTL`), keyed by **region and spec identity** — keyed on the spec
alone, two installations pointing one declaration at different accounts would
serve each other's observation. The final-readiness path bypasses the cache
entirely (`context["operation"]` is set only there), because serving a pre-fix
observation as proof of a post-fix state is wrong rather than merely stale.

### Rows, and the two ceilings that shape them

`system_readiness.run` keeps 64 checks per section, but that is not the binding
limit: `setup_safety.sanitize` bounds the **whole serialized report** to 256
items, shared across every registered section, and one detailed check row costs
a dozen. So the section follows the same shape the hosting sections use — a
summary row, then problem rows only:

| Row | When |
|---|---|
| `aws_infrastructure.mode` | always, first |
| `aws_infrastructure.environment` | only when the environment did not resolve |
| `aws_infrastructure.summary` | whenever an observation ran; counts are authoritative |
| `aws_infrastructure.<step>` | one per DAG step that is **not** `pass`, worst first, bounded to 12 |
| `aws_infrastructure.additional_steps` | when more steps need attention than fit |
| `aws_infrastructure.observation` | the observation itself failed |

A converged account costs exactly two rows. Detail rows are ordered by severity
**before** they are bounded, because both ceilings truncate from the end of the
list — a `fail` that sorted after twelve `warn`s would vanish while the warnings
survived, which turns a display limit into a correctness bug.

Finding status maps as follows. `PENDING` and `MANUAL` are real shipped statuses
and each has a reason for where it lands:

| `report` status | Readiness | Why |
|---|---|---|
| `PASS` | `pass` | |
| `DRIFT` | `warn` | exists, differs, and `apply` can modify it in place |
| `MISSING` | `pending` | not there yet; `apply` will create it |
| `MANUAL` | `warn` | exists and works, but the differing field is immutable — nothing this portal can do, and a red for an unrepairable state trains operators to ignore red |
| `PENDING` | `pending` | AWS is still building it; an Aurora cluster reports `creating` for ten minutes |
| `BLOCKED` | `fail` | a dependency failed, so this step never ran |
| `BLIND` | **split by cause** | see below |

`BLIND` splits the way `system_setup` already splits an exception with an
`iam_action`: a finding whose code ends in `.denied` names a permission the
provisioning credential does not have, which is a permanent operator problem and
reads `fail`. Everything else `BLIND` — a throttle, a timeout, an unreachable
endpoint — is `pending`. It still blocks green (a section nobody was allowed to
read must never look converged) without painting the whole Setup page red for a
transient AWS blip.

`ProviderCallError` becomes a `fail` row carrying `exc.detail()` and a
`Grant {iam_action} …` remediation. **Nothing escapes `check_infrastructure`** —
an escaping exception does not produce a worse row, it replaces the entire
section with `system_readiness.run`'s opaque `check_error`, losing the mode row
and every step with it.

### After a fresh provision, three AWS sections are unresolved by design

`aws_s3`, `aws_email` and `aws_monitoring` are still unresolved the first time an
operator logs into a freshly provisioned install, while `aws_infrastructure` is
already green. That is correct, not a broken report: the CLI builds the topology,
and the media bucket, verified SES domain and operations topic are adopted
afterwards through Fix Setup. Say so before an operator sees three reds beside
one green and concludes the green must be lying too.

### Tearing an environment down

Nothing in `mojo.deploy.provision` deletes — `discover.GuardedClient` blocks
every `delete_`/`terminate_`/`revoke_` verb at the seam, deliberately. Removing
an environment is therefore a manual operator task, and the order matters
because of dependencies:

1. Delete the DNS records and, if the CLI created it, the hosted zone.
2. Delete the load balancer, its listeners, and its target groups.
3. Terminate the nodes and release their elastic IPs.
4. Delete the Aurora cluster (take a final snapshot first, or you lose the data)
   and its subnet group.
5. Delete the ElastiCache replication group and its subnet group.
6. Empty and delete the config bucket, then the CloudTrail bucket.
7. Detach and delete the node role and instance profile, and the key pair.
8. Delete the security groups, then the VPC — subnets, gateway and routes go
   with it, and the VPC refuses while anything still uses it.
9. Remove `aws/environments/<env>.json` from the project, so
   `aws_infrastructure` stops observing an environment that no longer exists.

CloudTrail and GuardDuty are account-level and are deliberately **not** on that
list — turning them off is a security decision about the whole account, not part
of removing one environment.

### The CLI reads the ENV FILE, not this setting

`mojo.deploy.provision` reads `infrastructure_mode` from the committed
`aws/environments/<env>.json`, not from `INFRASTRUCTURE_MODE` in a settings
file, and it does not import `mojo/helpers/infrastructure.py` at all.

That is not a shortcut, it is the only thing that can work. The CLI provisions
an **empty account**: at the moment it runs there is no node, no
`/opt/api/var/django.conf`, and no Django settings for `settings.get_static` to
read — the setting this page documents does not exist yet, because the thing
that would hold it has not been created. `mojo/deploy/` is also barred from
importing `mojo.helpers.*` entirely (see `mojo/deploy/__init__.py`).

So `mojo/deploy/provision/inputs.py` restates the same two literals and the same
fail-closed value table, and a test in `tests/test_deploy/provision_cli.py`
imports **both** modules in one process — legal there, since a test process has
settings configured — and asserts they agree across the whole value table. A
duplicated fail-closed rule is only safe while something proves the copies have
not drifted.

The two are then joined at the other end: `inputs.infrastructure_mode(answers)`
is what the node-configuration step writes into `django.conf`'s
`INFRASTRUCTURE_MODE`, verbatim. The file is the source; the setting is its
image on a node that now exists. A CLI run launched with `--override-external`
still renders `external`, because the override is a property of one invocation
and never of the environment.

See [deploy/provision.md](../deploy/provision.md).

The **backstops exist for non-REST callers only** — a shell, a job, a future
importer. The REST gate has already answered HTTP for every ordinary caller, so
reaching a backstop means the gate was bypassed. The incident that fires there
is the point, not an accident.

`framework_overview` reports `can_update: false` and
`blocked_reason: "infrastructure_external"` in external mode, overriding the
other three reasons. The `installed` / `latest` / `pin` facts stay truthful —
knowing what runs here and what is published is a read, and reads are never
gated.

## What is deliberately NOT gated

Naming these is part of the contract; a reader who assumes "external mode blocks
all AWS writes" will be wrong.

- **S3 bucket operations** (`/api/aws/s3/...`) — create, empty, posture changes.
- **SES onboarding and reconcile.**
- **System Setup's fix operations.**
- **dnsman Route53 writes.**
- **Deploy retry / verify / converge** (`/api/account/admin/platform/deploy/*`).
  See the warning below — this one has a consequence.
- **The advanced/settings framework pin write** (`framework_pin` on
  `POST /api/account/admin/advanced/settings`). This is the **mitigation
  itself** and must stay open; gating it would take away the control an external
  installation needs most.

## External installs must pin `EDGE_FRAMEWORK_VERSION`

This is the one thing an external installation has to do beyond setting the
mode.

Deploy retry / verify / converge are **not** gated, and the framework version is
resolved at install time from the `EDGE_FRAMEWORK_VERSION` pin. With the pin
unset, a deploy retry installs whatever django-mojo is newest on PyPI —
a framework upgrade nobody asked for, arriving through a control that looks like
"run the same commit again".

So on an external installation, set the pin to `hold` (stay on the version
already proven on this fleet) or to an explicit version:

```
EDGE_FRAMEWORK_VERSION = hold
```

The portal writes this through Advanced → settings (`framework_pin`), which
stays open in external mode precisely so you can.

## Both directions are hazardous

- **External IaC against a `managed` installation**: the portal's mutations and
  the pipeline's applies fight. Whichever ran last wins, and the loser's change
  vanishes without a record on the side that made it.
- **Portal mutations against an `external` installation**: the next IaC apply
  reverts or replaces the live resource. An engine upgrade applied here and not
  in the IaC source is a change that will be undone, possibly during an outage
  window nobody chose.

The mode is the declaration of which one is true. Set it to match reality.

## How the portal learns the mode

`GET /api/account/admin/bootstrap` publishes it twice:

- `capabilities.infrastructure_managed` — a plain bool, mirrored into the
  `platform` and `webapps` feature lanes. (The feature-provider contract accepts
  named booleans only, which is why the mode *string* never rides in a feature.)
- `infrastructure: {"mode": ..., "managed": ...}` — a top-level fact, so a page
  can name the mode in words without re-deriving it.

Neither lane's `enabled` derives from the flag: it is true on every managed
install, and folding it into the lane's authority test would open the lane for a
caller holding none of the grants.

Portal JS treats a **missing** capability as managed. An older server that
predates this switch must not have its controls silently disabled; only an
explicit `false` takes them away.

## Preview

```
bin/admin_preview --infrastructure-mode external
```

The fixture bootstrap publishes the flag and the top-level key, and the
framework overview reports `blocked_reason: "infrastructure_external"` exactly
as production does.

## See also

- [Managed-service maintenance](maintenance.md)
- [Admin Platform](../account/admin_portal/platform.md)
- [Settings reference](../helpers/settings_reference.md)
- Web-developer view: [aws/infrastructure_mode](../../web_developer/aws/infrastructure_mode.md)
- Web-developer view of the readiness section: [account/system_setup](../../web_developer/account/system_setup.md#aws_infrastructure)
