# Infrastructure mode

Some installations own their AWS estate through this API; others have it applied
by an external IaC pipeline. On the second kind, the endpoints that would mutate
AWS refuse — and they refuse **everyone**, because this is a property of the
installation, not of the caller.

## Which endpoints refuse

| Endpoint | Behavior in external mode |
|---|---|
| `POST /api/aws/maintenance/apply` | 403 `infrastructure_external` |
| `POST /api/account/admin/platform/framework/update` | 403 `infrastructure_external` |

Every other endpoint is unchanged, including every read. The mode never hides
data — an out-of-support engine is still reported, the installed and published
framework versions are still reported, and the pin is still reported.

## The 403 body

```json
{
  "status": false,
  "error": "Applying an engine-version upgrade is disabled: INFRASTRUCTURE_MODE is external, so AWS infrastructure is managed by your infrastructure team, not this portal.",
  "error_code": "infrastructure_external",
  "data": {"mode": "external", "setting": "INFRASTRUCTURE_MODE"}
}
```

`error_code` is the stable field to branch on. `data.mode` and `data.setting`
name the state and the switch that produced it.

**More permissions will not help.** This is not a permission denial, and the
refusal is deliberately checked *before* the caller's grants so it cannot be
mistaken for one: a caller who is refused here would be refused as a superuser
too. The fix is a settings change on the server, made by whoever owns that
installation's infrastructure — not a grant.

Because the mode is checked server-side on every request, a tab that was loaded
while the installation was `managed` and submits after the flip is simply
refused. That is the intended outcome; re-read the bootstrap to resync the UI.

## Learning the mode

`GET /api/account/admin/bootstrap` publishes it two ways:

```json
{
  "capabilities": {
    "infrastructure_managed": false
  },
  "infrastructure": {
    "mode": "external",
    "managed": false
  },
  "features": {
    "platform": {
      "id": "platform",
      "enabled": true,
      "capabilities": {"view": true, "manage": true, "maintenance": true,
                       "infrastructure_managed": false}
    },
    "webapps": {
      "id": "webapps",
      "enabled": true,
      "capabilities": {"view": true, "manage": true, "onboard": true,
                       "infrastructure_managed": false}
    }
  }
}
```

- `capabilities.infrastructure_managed` — the bool, also mirrored into the
  `platform` and `webapps` feature lanes so a feature-scoped client does not
  have to reach outside its own namespace.
- `infrastructure.mode` — the string, top-level only. It never appears inside a
  feature's `capabilities`, which carry named booleans exclusively.

Two rules for clients:

1. **A missing flag means managed.** Test `!== false`, not truthiness. An older
   server that predates this switch does not publish the key, and treating that
   as "external" would disable working controls.
2. **Never derive lane visibility from it.** The flag is `true` on every managed
   installation, so it says nothing about what this caller may do. `enabled` and
   the authority capabilities are the only things that answer that.

## The framework overview

`GET /api/account/admin/platform/framework` reports the mode through the
`blocked_reason` field clients already read:

```json
{
  "schema_version": 1,
  "installed": "1.12.3",
  "latest": "1.13.0",
  "update_available": true,
  "pin": {"mode": "latest", "value": null},
  "can_update": false,
  "blocked_reason": "infrastructure_external"
}
```

`infrastructure_external` joins the existing `update_unavailable`,
`requires_superuser` and `no_converged_deployment`, and **overrides** them — it
is the reason the update endpoint would refuse whatever the version facts say.
Add it to your blocked-reason copy map and the existing "never offer a control
that would fail" path handles the rest.

## See also

- [aws/maintenance](maintenance.md)
- [account/admin_portal](../account/admin_portal.md)
- Backend view: [aws/infrastructure_mode](../../django_developer/aws/infrastructure_mode.md)
