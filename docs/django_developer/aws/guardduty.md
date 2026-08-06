# AWS GuardDuty Finding Ingestion

Reference documentation for backend developers routing Amazon GuardDuty findings
into the django-mojo incident engine.

Companion REST API reference: [docs/web_developer/aws/guardduty.md](../../web_developer/aws/guardduty.md)

The sibling receiver for CloudWatch alarms is documented in
[aws/cloudwatch.md](cloudwatch.md#sns-alarm-ingestion). The two receivers share
the signed-SNS preamble but have **independent topic allowlists**.

---

## The wire

GuardDuty does not publish to SNS directly. Findings go to EventBridge, and an
EventBridge rule forwards them to an SNS topic:

```text
GuardDuty -> EventBridge -> SNS -> POST /api/aws/guardduty/sns/finding
```

The EventBridge rule pattern:

```json
{
  "source": ["aws.guardduty"],
  "detail-type": ["GuardDuty Finding"]
}
```

Because EventBridge is the only route a finding can take to SNS, the SNS
`Message` is **always** an EventBridge envelope with the finding in `detail`.
The receiver validates `source` and `detail-type` before looking at the finding
— a cheap provenance check on a public endpoint.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `AWS_GUARDDUTY_FINDING_TOPIC_ARNS` | `[]` | Static exact SNS topic ARN allowlist for finding ingestion; empty denies every topic |

```python
AWS_GUARDDUTY_FINDING_TOPIC_ARNS = [
    "arn:aws:sns:us-east-1:123456789012:guardduty",
]
```

Read with `settings.get_static(..., [], kind="list")`, so it is **file-only** —
a DB/Redis `Setting` row cannot widen it — and a restart is required after a
change. It is deliberately separate from
`AWS_CLOUDWATCH_ALARM_TOPIC_ARNS`: a topic allowlisted for alarms must not be
able to confirm a subscription or deliver findings on this endpoint. The
receiver never honors `SNS_VALIDATION_BYPASS_DEBUG`; signature verification is
not optional here.

---

## Severity policy — nothing auto-opens an incident

| GuardDuty severity | Console label | Event level |
|---|---|---|
| `>= 9.0` | Critical | **6** |
| `>= 7.0` | High | **6** |
| `>= 4.0` | Medium | **6** |
| `>= 1.0` | Low | **4** |
| `>= 0.0` | Informational | **2** |
| outside `[0.0, 10.0]`, non-numeric, or `bool` | — | rejected (`400`) |

Two deliberate decisions are encoded here.

**Every band stays below `INCIDENT_LEVEL_THRESHOLD` (7).** `Event.publish()`
opens an incident when `rule_set or self.level >= INCIDENT_LEVEL_THRESHOLD`, so
a High or Critical finding mapped to level 8 would auto-open an incident from
the moment the receiver was enabled — and more than an incident. The
`elif created and LLM_API_KEY:` branch in `Event.publish()` is **not** gated by
`dispatch_handlers`, so a threshold-crossing event with no RuleSet still fires
an LLM triage job; `triage_new_incidents` sweeps every `status="new"` incident
regardless of ruleset; and a new incident carrying a `source_ip` calls
`geo_ip.update_threat_from_incident`, which at priority >= 7 stamps the address
`'medium'`. Keeping every band below 7 means **enabling the receiver only
records events**. This is the same choice already made for CloudWatch alarms
(`level=4 if new_state == "ALARM" else 2`). Escalation is opt-in, through a
RuleSet.

**Medium and above map to 6, not to 4.** `prune_events` deletes
`level__lt=6`, so level 6 is the floor that survives the retention window.
Low and informational findings are allowed to age out.

Integer severities are accepted (GuardDuty commonly emits `2`, `5`, `8`);
`bool` is excluded explicitly, because `isinstance(True, int)` is `True`.

---

## Event shape

| Field | Value |
|---|---|
| `scope` | `aws:guardduty` |
| `category` | `aws:guardduty:<finding type>` |
| `model_name` | `aws.guardduty.finding` |
| `model_id` | the rotating **occurrence** id (see below) |
| `level` | from the severity table above |
| `source_ip` | the remote address **only when it is an origin** |

`"aws:guardduty:"` is 14 characters and a finding type is bounded to 100, so
the category fits `Event.category` (`max_length=124`) with slack. An **unknown
finding type is accepted** — the type is data, not an enum, and a new AWS
detector family must never make the receiver return 400. Only charset
(`[A-Za-z0-9:/._-]`) and length bound it.

`Event.publish()` resolves a RuleSet by `scope` first, then `category`, then the
catch-all. Because a per-type category would need one RuleSet per finding type,
the policy is written against the **scope**.

### Bounded metadata

The raw finding is never persisted. Only these are stored:

- `finding_id`, `finding_type`, `detector_id`, `aws_account_id`, `aws_region`
- `severity`, `severity_label`, `guardduty_count`, `occurrence`
- `updated_at`, `created_at`, `resource_type`
- `identifiers` — at most **10** entries, each truncated to 256 characters,
  collected by walking the finding's `resource` block at most 3 levels deep and
  keeping ONLY these leaf names: `instanceId`, `accessKeyId`, `userName`,
  `userType`, `principalId`, `name`, `functionName`, `dbInstanceIdentifier`,
  `username`, `arn`.
- `action` — a fixed per-type key set:
  `networkConnectionAction` (direction, protocol, blocked, local/remote port),
  `awsApiCallAction` (api, service, caller type, error code),
  `dnsRequestAction` (domain, protocol, blocked),
  `portProbeAction` (blocked),
  `kubernetesApiCallAction` (verb, request URI).
- `remote_ip` / `remote_ip_is_origin` when the action carries a peer address.

The allowlist **is** the control: `record_event`'s `sanitize_dict` strips known
sensitive key names but is not GuardDuty-aware, so anything not on the list —
tags, arbitrary blobs, unexpected credential-shaped keys — is simply dropped.
The AWS region is stored as `aws_region` rather than `region` because
`Event.sync_metadata` writes a geo-lookup `region` for events that carry a
`source_ip`.

### `source_ip` — origin vs destination

The remote address becomes the Event's `source_ip` only when it is an **actor**:

| Action | Origin? |
|---|---|
| `networkConnectionAction` with `connectionDirection: "INBOUND"` | yes |
| `networkConnectionAction` with `connectionDirection: "OUTBOUND"` | **no** |
| `awsApiCallAction` | yes |
| `portProbeAction` | yes |
| `kubernetesApiCallAction` | yes |
| `dnsRequestAction` | no address |

For an outbound connection the remote address is a destination **our own host**
chose to contact. It goes into `metadata["remote_ip"]` and nothing else:
feeding a C2 destination (or an innocent third party) into inbound threat
scoring would poison `GeoLocatedIP` threat levels and could route it into a
`block://` handler.

---

## Dedupe and the occurrence contract

`GuardDutyFinding` is the durable row, one per
`sha256(account:region:detector_id:finding_id)`. The hash covers all four parts
because GuardDuty ids are unique only **per detector**, and fanning several
accounts and regions into one topic is the normal deployment shape. Identity is
re-asserted after the row lock is taken.

| Field | Meaning |
|---|---|
| `finding_key` | the SHA-256 identity |
| `last_updated_at` | the monotonic dedupe watermark |
| `occurrence_count` | deliveries that changed state |
| `active_incident` | the live incident, or null |
| `opening_event` | the Event naming the live occurrence |
| `pending_event` | the Event whose handler dispatch is outstanding |
| `dispatch_status` | `pending` / `complete` |

- A delivery whose `updatedAt` is **at or before** the watermark is a
  duplicate: no Event, no state change, `{"duplicate": true, ...}`. This covers
  both SNS replays and out-of-order deliveries; the watermark never regresses.
- A new delivery with **no live incident** publishes with `use_catchall=False`
  and `dispatch_handlers=False`, then records the resulting incident and (when
  a handler is configured) marks the dispatch pending.
- A new delivery with a **live incident** links the Event to it and escalates
  the incident priority if the band rose. There is no handler re-dispatch —
  the same contract CloudWatch applies to a repeat transition.
- Handler dispatch happens **outside** the transaction, with the idempotency
  prefix `aws-gd:<finding pk>:<event pk>`. The key is per **occurrence**, not
  per finding: `Job.idempotency_key` is globally unique and `jobs.publish`
  returns the pre-existing job on collision (logging only at `info`), so a
  finding-only prefix would make every occurrence after the first publish
  nothing at all, silently.
- A dispatch stranded by a crash is retried at the start of the next delivery,
  **before** the transaction. A failure there is logged and swallowed so the
  new occurrence is still recorded — otherwise one permanently failing dispatch
  would drop every later delivery for that finding at the door.

### Why `model_id` rotates

`opening_event` names the live occurrence, and every Event of that occurrence
carries its id as `Event.model_id`. When the incident goes terminal
(`resolved` / `closed` / `ignored`, or pruned — the FK is `SET_NULL`), both
`active_incident` and `opening_event` are cleared, so the next delivery mints a
fresh occurrence id.

This is load-bearing. `determine_bundle_criteria` has **no status filter**, and
with `bundle_minutes=None` it adds no time filter either, so the criteria reduce
to `{category, model_name, model_id}` over all history. Without rotation, a
finding that recurred a month after its incident was resolved would bundle
straight back into the **resolved** incident and silently reopen closed work.

---

## Durable data model

`GuardDutyFinding` is read-only through its generated REST surface
(`CAN_CREATE`, `CAN_UPDATE`, `CAN_DELETE` are all false) and its `RestMeta`
requires `manage_aws` or `security`. Like `CloudWatchAlarm`, **no REST endpoint
is registered for it** — it is internal lifecycle state, not an API resource.

---

## Incident policy

Ingestion is opt-in at the policy layer. Install the shipped policy:

```python
from mojo.apps.incident.models import RuleSet

RuleSet.ensure_guardduty_rules()
```

It creates `category="aws:guardduty"`, `bundle_by=MODEL_NAME_AND_ID`,
`bundle_minutes=None`, `handler="notify://perm@manage_security"`, gated on
`level >= 6` (Medium and above). It is **not** part of
`ensure_default_rules()`, so non-AWS deployments never get it.

`MODEL_NAME_AND_ID` is safe here only because of the rotating occurrence id
described above.

Or write your own:

```python
from mojo.apps.incident.models import BundleBy, RuleSet

RuleSet.objects.create(
    category="aws:guardduty",
    name="Production GuardDuty findings",
    bundle_by=BundleBy.MODEL_NAME_AND_ID,
    bundle_minutes=None,
    handler="ticket://?priority=8&category=security&board=3",
    trigger_count=None,
)
```

Without any GuardDuty RuleSet the whole dispatch path
(`dispatch_status` / `pending_event` / `_dispatch`) is inert, because
`should_dispatch` is only ever set inside the `if rule_set:` branch of
`Event.publish()`.

---

## AWS setup and verification

1. Enable GuardDuty in each account/region you want to ingest.
2. Create a dedicated SNS topic and put its exact ARN in
   `AWS_GUARDDUTY_FINDING_TOPIC_ARNS`. Its topic policy should grant
   `sns:Publish` only to EventBridge; do not reuse a topic that application
   principals — or CloudWatch alarms — can publish to.
3. Create the EventBridge rule with the pattern at the top of this document and
   target the topic.
4. Subscribe the HTTPS endpoint and wait for the signed confirmation to succeed.
5. Add a GuardDuty RuleSet if findings should become incidents or tickets.
   Register Maestro first with `python manage.py register_maestro` when using
   `ticket://?board=<id>`.
6. Verify end to end with real, AWS-signed deliveries rather than by forging an
   SNS signature:

```bash
aws guardduty create-sample-findings \
  --detector-id <detector-id> \
  --finding-types "UnauthorizedAccess:EC2/SSHBruteForce"
```

Sample findings traverse the same EventBridge rule, topic, signature and
allowlist as real ones. Confirm an `Event` with
`scope="aws:guardduty"` appears, and a `GuardDutyFinding` row with
`occurrence_count = 1`.

---

## Module layout

```
mojo/apps/aws/rest/sns.py                       # signed SNS receivers (shared preamble)
mojo/apps/aws/services/sns.py                   # SNS verification and confirmation
mojo/apps/aws/services/guardduty_findings.py    # normalization and lifecycle
mojo/apps/aws/models/guardduty_finding.py       # durable per-finding state
```

## Testing

Tests live in `tests/test_aws/guardduty_ingress.py`. They call the view
in-process with `RequestFactory` and sign real envelopes with a throwaway RSA
key, so no AWS credentials are needed.
