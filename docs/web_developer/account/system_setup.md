# System Setup API

The built-in Admin's **System Setup** page is the supported browser workflow
for checking and repairing an installation. These endpoints are intentionally
narrower than normal administrator APIs: the caller must be an active literal
superuser using an interactive Bearer JWT. API keys, group tokens, inactive
users, and permission-only non-superusers receive `403`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/account/admin/setup/options` | Sections and an active fix operation |
| `GET` | `/api/account/admin/setup/readiness?section=<code>` | Run all or one read-only report |
| `POST` | `/api/account/admin/setup/create` | Create/replay a `check` or `fix` operation |
| `GET` | `/api/account/admin/setup/detail?operation=<uuid>` | Reload/resume an operation |
| `POST` | `/api/account/admin/setup/advance` | Execute or reconcile one step |
| `POST` | `/api/account/admin/setup/choose` | Supply the current typed choice |
| `POST` | `/api/account/admin/setup/cancel` | Cancel between steps |

Operation creation and all advance/choose/cancel calls require authentication
within 600 seconds. HTTP `440 reauth_required` means request explicit operator
confirmation and return through Bouncer with `force_reauth=1`; do not present
it as a logout. The existing session remains valid and Cancel may preserve the
page. After step-up, reconcile the durable operation before continuing. Mutable
calls also require the same browser
`Origin` that created the operation. Do not synthesize or forward a different
Origin.

The built-in page additionally requires the private Admin source session before
the browser can download `/admin/assets/features/platform/page.js`. Obtain it through
`POST /api/account/admin/session` as described in
[Admin Portal API Guide](admin_portal.md). That cookie is only a source-delivery
gate; every endpoint below still requires the interactive JWT and revalidates
the literal superuser.

## Built-in page behavior

The packaged page is a thin client for this protocol. It presents one primary
Fix all journey plus scoped checks, resumes `active_fix`, renders late choices
from the returned JSON schema, shows durable step progress, and displays the
final readiness rerun. Check codes and the bounded operation log stay under
closed Technical details disclosures. Initial reads use skeleton rows;
mutations use one non-dismissible busy layer and disable duplicate actions.
Every failure becomes visible operator feedback. A lost fix-create response is
reconciled from `options.active_fix` with the retained replay key; the browser
never automatically retries an uncertain mutation. It never shells out, calls a
management command, or repeats setup service logic in JavaScript.

When BASE_URL is the pending choice, the page proposes the current origin for
a direct HTTPS Admin session. The local live-QA bridge proposes its already
validated and pinned upstream through a gate-protected local context endpoint,
so `http://localhost` is never mistaken for the public API. The value remains a
prefill only: the operator submits it through the unchanged `choose` request,
and the server performs its normal protected validation before saving it.

There is no duplicate Network & Hosting directory. Each failing row has at most
one action: configure its durable Setup choice, open the first-class owner, or
change a deployment setting. `django.base_url` is the only Django check that
offers Configure BASE_URL; the rest never inherit a misleading section-wide
Fix action. Owner links do not grant authority and Setup does not invoke their
mutation APIs. WebApp deployment-key tokens are never accepted, read, or
rendered on this page.

## Request schemas

| Endpoint | Request |
|---|---|
| `options` | No fields |
| `readiness` | Optional query `section=<stable-section-code>` |
| `create` | JSON `mode` (`check` or `fix`); optional `section`; optional `replay_key` up to 128 characters; same-origin `Origin` header |
| `detail` | Query `operation=<operation-uuid>` (the alias `operation_id` is accepted) |
| `advance` | JSON `operation`; bound `Origin` header |
| `choose` | JSON `operation`, `step_id`, `definition_version`, `choice_revision`, and object `choice`; bound `Origin` header |
| `cancel` | JSON `operation`; bound `Origin` header |

`section` must be one of the codes returned by `options`. Omitting it means all
sections. Generate a fresh `replay_key` for each create intent and retain it
until the create response arrives; retrying the same actor/mode/section/Origin
and key returns the original operation with `replayed: true`.

`options` returns:

```json
{
  "schema_version": 1,
  "sections": [
    {
      "code": "django",
      "label": "Django installation",
      "fixable": false,
      "choice_schema": null
    }
  ],
  "active_fix": null
}
```

`active_fix`, when present, has the operation shape below.

## Run checks

```http
POST /api/account/admin/setup/create
Origin: https://admin.example.com
Authorization: Bearer <interactive-superuser-jwt>
Content-Type: application/json

{"mode":"check","replay_key":"client-generated-uuid"}
```

Call `advance` once with the returned operation id. The terminal operation has
`status: "succeeded"` even when the readiness report contains a failed check;
the operation successfully measured the system. Use `report.overall` for
readiness.

Reports use `schema_version: 1` and the statuses `pass`, `warn`, `fail`, and
`pending`. Every check includes `code`, `explanation`, `remediation`,
`fixable`, `required_choice`, and optional bounded scalar `details`. The full
report shape is:

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-10T03:00:00+00:00",
  "overall": "fail",
  "summary": {"pass": 6, "warn": 0, "fail": 1, "pending": 0},
  "sections": [
    {
      "code": "django",
      "label": "Django installation",
      "status": "fail",
      "fixable": true,
      "checks": [
        {
          "code": "django.base_url",
          "status": "fail",
          "explanation": "The public API address is not configured.",
          "remediation": "Configure the canonical public HTTPS API origin.",
          "fixable": true,
          "required_choice": null
        }
      ]
    }
  ]
}
```

Aggregate status uses the severity order `fail`, `pending`, `warn`, then
`pass`. `GET readiness` returns this report directly. A check operation stores
the same shape in `operation.report`.

`django.local_request` is returned only when the deployment explicitly sets
`SYSTEM_SETUP_LOCAL_API_URL`; its bounded `details.target_source` is then
`configured_static`. Request-port and port-80 guesses are node diagnostics, not
operator readiness, and are omitted. Missing `STATICFILES_DIRS`/`STATIC_ROOT`
also produce no Setup check: they are optional deployment inputs and normal
`collectstatic` owns output creation when static serving is enabled. The
packaged browser filters these two legacy rows too, so it remains accurate when
temporarily pointed at an older live backend.

## Fix and resume

Create with `mode: "fix"`, then call `advance` one step at a time. Persist only
the operation id in browser state. The server owns the cursor, definition versions,
choice revisions,
lease, log, and report.

When `status` is `waiting_for_choice`, render the exact
`current_step.choice_schema`. Submit:

```json
{
  "operation": "8d266835-1c5b-4434-9eb3-bb559b51ac64",
  "step_id": "base_url",
  "definition_version": 1,
  "choice_revision": 0,
  "choice": {"base_url": "https://mojo.example.com"}
}
```

The choice is stored once under a row lock. A stale definition version or
choice revision, wrong step,
duplicate choice, unsupported field, or changed operation returns `409`/`400`
without moving the cursor. Reload with `detail`, render the returned current
step, and continue. Never replay an old choice optimistically.

Fix steps may remain `reconciling` after a successful mutation. This is
expected: the next `advance` proves provider state before marking the step
`proven`. An interrupted `mutation_attempted` step reconciles instead of
blindly repeating the provider write.
An unexpected fixer or reconciliation exception leaves the operation active as
`reconciling`; it does not mean the mutation failed. The safe log includes the
exception class and, only for a proved AWS authorization denial, the exact
missing IAM action. Continue calling `advance` so authoritative reconciliation
can prove the outcome. A denied AWS mutation or a server-side typed definitive
failure can move the step directly to `failed`.
Neither `mutation_attempted` nor `reconciling` can be cancelled, even when its
lease has expired: the provider outcome must first be reconciled. If an
installed upgrade changes a step's registered `definition_version`, both
choose and advance return `409` for planned/waiting steps; cancel from that safe
state and start a new operation. An uncertain old-version step cannot be
cancelled and does not return to its fixer: the server invokes the registered
read-only reconciliation adapter for that exact version until it is proven.

The operation log is bounded and safe to render as text. It never contains
credentials or reveal-once secrets. Treat any future secret/provider-specific
entry as a server defect; do not create UI that depends on such values.

All operation endpoints return this shape; `create` adds `replayed`:

```json
{
  "id": "8d266835-1c5b-4434-9eb3-bb559b51ac64",
  "mode": "fix",
  "section": null,
  "status": "waiting_for_choice",
  "cursor": 1,
  "steps": [
    {
      "id": "base_url",
      "definition_version": 1,
      "choice_revision": 0,
      "label": "Configure public BASE_URL",
      "kind": "base_url",
      "section": "",
      "state": "waiting_for_choice",
      "choice_schema": {
        "type": "object",
        "properties": {"base_url": {"type": "string", "format": "https-origin"}},
        "required": ["base_url"],
        "additionalProperties": false
      }
    }
  ],
  "current_step": {
    "id": "base_url",
    "definition_version": 1,
    "choice_revision": 0,
    "label": "Configure public BASE_URL",
    "kind": "base_url",
    "section": "",
    "state": "waiting_for_choice",
    "choice_schema": {
      "type": "object",
      "properties": {"base_url": {"type": "string", "format": "https-origin"}},
      "required": ["base_url"],
      "additionalProperties": false
    }
  },
  "choices": {},
  "report": {},
  "log": [
    {
      "at": "2026-08-10T03:00:00+00:00",
      "code": "choice.required",
      "message": "Choice required for base_url"
    }
  ],
  "created": "2026-08-10T02:59:58+00:00",
  "modified": "2026-08-10T03:00:00+00:00",
  "finished_at": null
}
```

`current_step` is the full step at `cursor`, or `null` after the cursor passes
the final step. Operation statuses are `planned`, `running`,
`waiting_for_choice`, `reconciling`, `succeeded`, `failed`, and `cancelled`.
Step states are `planned`, `waiting_for_choice`, `mutation_attempted`,
`reconciling`, `proven`, and `failed`. A check operation can be `succeeded`
while `report.overall` is not `pass`; a fix operation terminates `failed` when
its final proof is `warn`, `pending`, or `fail`. Only an all-`pass` final report
is a successful fix.

Responses and durable operation state share one sanitizer. It enforces bounded
depth, item count, string length, and total serialized bytes; recognizes
credential/token/private-key/JWT/AWS-key and unlabeled high-entropy opaque
material even under innocent field names; pre-bounds huge strings before
inspection; and removes URL userinfo and query values, including presigned queries.
At a bounded collection edge, the remaining entries are omitted and the root
envelope carries `truncated: true`; typed `sections` and `checks` arrays never
receive a scalar sentinel. A `"[truncated]"` value can still replace an
overlong or over-deep scalar leaf. Clients should render the retained rows and
tell operators that the report is partial. They should also ignore malformed
section/check entries from older servers rather than failing the whole view.

The framework envelope is included in that bound without consuming ordinary
scalar check details or choice enum values. Deeper provider-owned structures
still truncate. The packaged Admin presents `mutation_attempted`,
`reconciling`, and `proven` as `Applying changes`, `Verifying changes`, and
`Complete`, then states explicitly when changes were applied and verified.

## AWS sections and choices

AWS integration adds five section codes to `options`:

| Code | Fixable | Behavior |
|---|---:|---|
| `aws_identity` | No | Reports the selected AWS identity, account, and region through bounded STS inspection. |
| `aws_s3` | Yes | Discovers conservative private media-bucket candidates and adopts the exact selected bucket as the private system FileManager. |
| `aws_email` | Yes | Imports an existing verified SES domain, selects its system sender, and installs only missing shipped templates. |
| `aws_monitoring` | Yes | Creates or explicitly adopts the owned operations topic, HTTPS subscription, CloudWatch alarm profile, and delivery proof. |
| `aws_infrastructure` | **No** | Read-only observation of the provisioned VPC/database/cache/node topology, plus the installation's infrastructure mode. |

### `aws_infrastructure`

Its first check is always `aws_infrastructure.mode`, which reports whether this
portal owns the installation's AWS estate. On an installation running
`INFRASTRUCTURE_MODE = external` that row is **`warn`**, and its explanation is
the same sentence the gated endpoints return in their 403 body. The section
never turns that into a failure and never blocks anything — it is a statement
about who owns the infrastructure, not a defect. See
[aws/infrastructure_mode](../aws/infrastructure_mode.md).

The section is **not fixable**, and `fixable` is `false` on every one of its
rows. "Fix all" cannot repair infrastructure and does not try; converging the
topology is the provisioning CLI's job, run by an operator with a shell.

Its remaining rows follow the same summary-then-problems shape as the hosting
sections. `aws_infrastructure.summary` is authoritative — render its counts even
when the detail rows below it are bounded — and detail rows appear only for
provisioning steps that are not ready, worst first, with an
`aws_infrastructure.additional_steps` row when more need attention than fit.

Two states are normal rather than broken, and a client should not present either
as an error:

- **`pending`, with a single `aws_infrastructure.environment` row.** This
  installation's infrastructure was not provisioned by django-mojo — there is no
  `aws/environments/<env>.json` to observe, or several exist and nothing names
  which one this installation is. No AWS call is made in that state at all.
- **`aws_s3`, `aws_email`, and `aws_monitoring` unresolved on a freshly
  provisioned install, while `aws_infrastructure` is already green.** That is
  by design: the CLI builds the topology, and the media bucket, verified SES
  domain, and operations topic are adopted afterwards through Fix Setup. An
  operator seeing three unresolved AWS sections next to one green one has a
  correct report, not a broken one.

Hosting integration adds six non-fixable readiness sections. Their
remediation links should open the normal guarded Domains, Certificates,
Vhosts/Routes, Fleet, or WebApp key workflows — except `webapp_destination`,
whose own remediation is completing System Setup's `BASE_URL` step (or, for a
split serving topology, the file-only `EDGE_WEBAPP_CNAME_TARGET` override), and
`apps_domain`, whose remediation is onboarding a web app under the resolved
domain (or converging it by hand from Domains). System Setup never mints a
reveal-once deployment token in a report.

| Code | Ready only when |
|---|---|
| `hosting_dns` | Managed domains can change DNS, certificates are active/unexpired, delegated ACME is verified, and no challenge reservation remains live |
| `hosting_vhosts` | At least one enabled Vhost has an active domain and certificate, and every enabled Vhost passes that check |
| `edge_fleet` | Every node/pool in `EDGE_EXPECTED_TOPOLOGY` answers from an `edge`-channel runner with the expected django-mojo version and desired generation, and its combined serving generation equals the live generation, with no excluded or pending material |
| `webapp_keys` | Every WebApp's safe key metadata is active; missing is `pending`, inactive is `fail`, and revoked is `warn` |
| `webapp_destination` | A guided WebApp address has somewhere to point: the `EDGE_WEBAPP_CNAME_TARGET` override or the platform's own `BASE_URL` hostname resolves. Unconfigured is `pending`; a set-but-unusable override is `fail` |
| `apps_domain` | The installation's [apps domain](../../django_developer/edge/webapps.md#the-apps-domain) — the domain new web apps go live under with zero per-app DNS work — already has its wildcard CNAME and covering certificate. No qualifying domain yet, or one that hasn't converged, is `pending`; a DNS-read error is `fail` |

The fleet section never treats a missing topology, node, response, pool, or
generation as green. WebApp checks return only bounded metadata such as
`webapp`, `linked`, `active`, and `last_action`; no token/hash/ciphertext can be
recovered through Setup. The destination check's `details` carry only the
resolved hostname and its `provenance` (`override` or `platform_base_url`) —
never a token or credential, since none is involved.

Every hosting section's first check is a global summary over all matching rows;
only the subsequent problem-detail rows are bounded to 16. Render the summary
counts as authoritative, including failures beyond that limit. WebApp summary
details contain status counts and `action_<mint|rotate|revoke>` receipt counts;
per-WebApp `webapp`, `linked`, `active`, and `last_action` detail appears only
for non-green rows. Neither shape contains token material.

Render the server-returned `choice_schema`; do not build a second discovery UI.
The packaged Admin presents S3 as an existing-bucket selector with a clear
affirmative action and explains that objects and unrelated configuration are
preserved. For rolling-upgrade compatibility only, when an older backend omits
the bucket enum, the client reads the already-authorized
`GET /api/aws/s3/bucket` inventory to populate the selector. The Setup service
still rediscovers and validates the exact choice before changing it; a current
backend's empty safe-candidate enum remains empty and is never replaced by raw
inventory. The current choice objects are:

```json
{"bucket":"existing-media","adopt_existing":true}
```

`bucket` is an exact enum. Public ACL/policy/status, foreign-owner, website,
cross-region, tenant/user, wildcard/federated/unknown-policy, or unclassifiable
buckets never appear. The adoption step separately rejects a bucket already
tagged to another installation. Adoption keeps all four S3
Block Public Access flags enabled, creates no public bucket policy, and merges
wildcard presigned-upload CORS without deleting unrelated rules.

```json
{"domain":"verified.example","sender":"ops@verified.example"}
```

`domain` is an exact enum of fully paginated SES identities with verification
status `Success`. `sender` must be a valid address on that domain. Existing
customized email templates remain unchanged because Setup installs only missing
shipped templates.

```json
{
  "topic_arn":"arn:aws:sns:us-east-1:123456789012:django-mojo-example-operations",
  "adopt_existing_topic":true
}
```

The monitoring choice is present only when the exact reserved topic name
already exists without django-mojo ownership tags. It is affirmative legacy
adoption, not a free-form ARN field. Partial or conflicting ownership tags fail
closed. Adoption also fails before tagging or allowlisting when any existing
publish-capable Allow is not same-account/default-owner or an exact bounded
CloudWatch source grant. Setup preserves safe unrelated SNS policy statements,
creates the exact HTTPS subscription and repairs the complete owned alarm
profile, and remains `reconciling` until the persisted random challenge for
this operation delivers an ordered ALARM then OK after its stable cutoff. That probe is
evidence-only: it does not create an Event, Incident, Ticket, or normal rule
dispatch.

An AWS provider-failure `details` object may contain only bounded scalar
evidence: `operation`, `provider_code`, `retryable`, `mutation_state`, optional
safe `request_id`, and `iam_action` only for an authorization denial. Successful
checks may also include their documented bounded domain fields. Display the
exact denied IAM action as remediation. Never expect raw AWS messages,
credentials, provider payloads, or request parameters.

## Protected settings

`BASE_URL`, installation identity, monitoring topic ownership, and expected
edge topology cannot be created, updated, renamed, or deleted through generic
`/api/settings`, regardless of permission. Use System Setup. Arbitrary Django
settings editing is not part of this API.

## Status handling

- `401`: JWT missing or expired.
- `400`: invalid mode, section, choice schema/value, or operation input.
- `403`: not an active literal superuser, machine credential, or Origin mismatch.
- `404`: operation id does not exist.
- `409`: active fix conflict, stale choice, active lease, or terminal operation.
- `440`: recent interactive authentication required.
