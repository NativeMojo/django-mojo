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

Two endpoints, each at both layers:

| Endpoint | REST gate | Service backstop |
|---|---|---|
| `POST /api/aws/maintenance/apply` | `mojo/apps/aws/rest/maintenance.py` | `maintenance.apply_upgrade` raises `MaintenanceError(..., "infrastructure_external", 403)` |
| `POST /api/account/admin/platform/framework/update` | `mojo/apps/account/rest/admin_platform.py` | `admin_platform.apply_framework_update` raises `PermissionDeniedException` |

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
