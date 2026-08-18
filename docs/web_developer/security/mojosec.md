# MojoSec Sensor Ingestion

`POST /api/incident/mojosec/batch` is the machine-facing receiver for dedicated
EC2 host sensors. It is not a browser/admin ingestion endpoint and does not use
the normal REST response envelope.

## Authentication

Each installation uses its own API key:

```http
Authorization: apikey <installation-token>
Content-Type: application/json
Content-Encoding: gzip
```

The key must carry `mojosec_ingest`, belong to an effectively active group, and
have a protected server-side `metadata.protected.mojosec` profile that is enabled, names
the same `sensor_id` as the batch, and allows the submitted protocol version.
JWTs, ordinary or unenrolled API keys, keys under an inactive group chain, and
sensor-ID/version mismatches receive `403`.

## Request

The body is the strict `mojosec.batch` v1 contract:

```json
{
  "schema": "mojosec.batch",
  "version": 1,
  "sensor_id": "web-prod-i-0123456789",
  "sent_at": "2026-08-08T12:00:10Z",
  "policy_revision": "baseline-1",
  "events": [{
    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "kind": "auth.ssh_login",
    "observed_at": "2026-08-08T12:00:00Z",
    "first_seen": "2026-08-08T12:00:00Z",
    "last_seen": "2026-08-08T12:00:00Z",
    "severity": "high",
    "summary": "SSH login accepted",
    "count": 1,
    "attributes": {"source_ip": "192.0.2.20", "user": "deploy"},
    "recommendation": "review"
  }]
}
```

Every field shown is required and unknown fields are rejected. Timestamps must
be timezone-aware ISO-8601 values with
`first_seen <= last_seen == observed_at`. Severity is `info`, `warning`,
`high`, or `critical`; recommendation is `none`, `review`, or `block_ip`.
Event IDs are unique lowercase SHA-256 digests within a batch. A batch contains
1–500 events and both its compressed wire body and decompressed JSON are capped
at 512 KiB.

On a MojoSec-enabled standard or Edge nginx deployment, the exact receiver
location also enforces a 512 KiB request-body cap before Django. This is a
defense in depth limit; clients must still obey the receiver's compressed and
decompressed limits described below.

The receiver streams at most 512 KiB plus one byte and never exposes this body
to generic request parsing or request/DB logging. `Content-Length`, when sent,
must match the bytes read. Broad request logging records only a fixed
`mojosec_batch` sensitive-body marker.

The body may be plain JSON or exactly one gzip member. Concatenated/trailing
gzip data, duplicate JSON keys, non-finite numbers, invalid UTF-8, unknown
schema fields, and unsupported content encodings are rejected. The
authoritative compatibility example is the checked-in
`tests/test_mojosec/golden/batch_v1.json` fixture.

## Acknowledgements

The success response is the exact shared acknowledgement document:

```json
{
  "schema": "mojosec.ack",
  "version": 1,
  "results": [
    {"id": "<64-character-lowercase-sha256>", "status": "accepted"}
  ]
}
```

Statuses are per event:

| Status | Sensor behavior |
|---|---|
| `accepted` | Remove from the local spool. |
| `duplicate` | Remove; the same identity and digest already reached a terminal receipt (which need not have an Event). |
| `rejected` | Remove; the evidence is permanently invalid or conflicts with the ID. Also covers storage-impossible input (NUL/lone-surrogate text in `policy_revision`, an event, or a field the database rejects) and evidence pruned by retention before publication — each with a distinguishing `reason`. |
| `retry` | Keep and retry with bounded backoff. |

Missing result IDs, malformed acknowledgements, and non-2xx responses are also
retryable. The receiver derives central categories and severity; a host
`recommendation` never performs an action directly. Only an exact central
RuleSet such as `mojosec.web.probe` may promote the Event to an Incident and
run a handler. Host severity cannot raise the server-owned level. Known
source-bearing SSH, reliably attributed sudo, and web kinds populate the
Event's canonical source IP; for `web.probe` and `web.denied` the sensor
fingerprint includes the caller identity, so interleaved callers/hosts/
methods/statuses do not collapse. `web.error` events instead aggregate across
callers — one outage is one growing event per failure shape — and their
`Event.source_ip` carries a single latest-occurrence sample: a witness of the
failure, not an actor attribution. The source alone still performs no action:
only an exact active central RuleSet may create an Incident or run a handler.

Raw bounded request targets, referrers, and user agents are retained in the
protected `MojoSecReceipt.replay_features` audit record (`DENY_AI`, excluded
from the default graph). Event metadata contains a
central allowlisted projection: queryless/token-normalized path, canonical
method/host/status/upstream values, HTTP(S) referrer origin plus safe path,
structured UA family/major/digest plus centrally scrubbed display, request ID,
protocol/TLS, ports, byte counts and upstream measurements. For
`auth.sudo_command`, the existing `view_security`/`security` Event surface also
returns exact accepted `command`, `command_path`, and `cwd`, plus actor, target
user, TTY, boot ID, audit session, and explicit attribution. For every valid,
non-truncated `command_path`, `command_family` is present as a server-owned
known family or literal `unknown`; invalid, missing, and truncated paths omit
it. Literal
`command_truncated`, `command_path_truncated`, and `cwd_truncated` booleans are
present only when the corresponding sensor value is a retained prefix. If a
marker key is present with `false`, a truthy number/string, or any value other
than literal boolean `true`, the marker and its paired field are omitted. The
full-value digest remains receipt-only; secret-looking command arguments are
not redacted for authorized administrators.
For system events (`system.service_error`, `system.oom`) the projection
carries only the validated failed-unit name and failure kind; raw journal
message text never projects.
The native nginx stream never collects bodies, cookies, the Authorization
header, or arbitrary headers. Bounded web request targets, referrers, and user
agents receive centrally scrubbed projections. Sudo command evidence follows
the separate admin contract above: the receiver enforces byte bounds and omits
malformed/non-string/NUL values without stripping, normalizing, truncating, or
redacting accepted command text.

### Native sensor evidence limits and attribution

The protocol rejects an event whose encoded `attributes` exceed 8 KiB. The
built-in sensor uses a closed per-kind allowlist and reserves 512 bytes, so it
emits at most 7,680 bytes before spooling or sending. Its raw nginx request
target is bounded to 2,048 bytes and the raw referrer and user agent to 1,536
bytes each; a raw sudo command is capped at 2,048 bytes, with 512-byte working
directory and executable-path caps. Truncated retained values carry
`<field>_truncated: true` and a full-value `<field>_sha256`; lower-priority
fields may be absent once the total budget is full.

For nginx observations, the native sender also supplies request ID,
scheme/protocol/TLS, client/direct-peer/server ports, request/response bytes,
and upstream connect/header/response timing and length/byte measurements.
Optional empty or `-` upstream entries are absent. The raw request
target/referrer/user-agent values stay in
the protected receipt; the Event receives only the projection described above.
User-agent text alone is never a detector signal.

Accepted SSH logins establish a `(boot_id, audit_session)` source mapping.
The mapping source must be a successful trusted Linux audit-transport
`USER_START`/`USER_LOGIN` record for root-UID sshd with `terminal=ssh`;
message text and a caller-chosen syslog identifier are insufficient.
Sudo uses it only when actor and TTY are compatible; the mapping is retained
for 30 days with a 4,096-row cap. Without an exact audit-session mapping,
source attribution is allowed only for one fresh (five-minute), exact
actor-plus-TTY `who` row.
Conflicting identity for one audit key becomes a sticky ambiguous tombstone.
`who` itself is streamed under fixed locale, timeout, byte, and line caps.
`attribution_provenance` is `audit_session`, `who`, or `none`; only the first
two allow sudo's address to populate `Event.source_ip`. The projected
`attribution` is always explicit: audit-session promotion requires a valid IP,
sensor-shaped actor and boot-ID strings, and an audit session, while `who`
promotion requires a valid IP plus sensor-shaped actor and TTY strings. An
incomplete, malformed, or non-string tuple becomes `none` and leaves
`source_ip` null, even if the receipt contains a stray address. Stale, reused,
or ambiguous rows therefore remain unattributed.

The current AL2023 split-OpenSSH executable and non-root invoking sudo UID are
accepted only under exact kernel/journal provenance. `auth.session_open` is an
informational local PAM service session—not an SSH login—and can use only exact
audit-session attribution. A sudo event records one sudo invocation; it does
not claim to capture commands later typed inside `sudo -s`.

The one complete root-produced `systemd-user` PAM lifecycle is retained as
local sensor health rather than fleet incident evidence. The exact classifier
requires literal `kind="auth.session_open"`, `severity="info"`,
`summary="PAM service session opened"`, and `recommendation="none"`,
a canonical lowercase 64-hex fingerprint or event ID, observation
`aggregate=false` or wire `count=1` with equal first/last/observed timestamps,
canonical target user/UID, root opener and producer, target UID equal to audit
login UID, positive PID, exact systemd comm/executable/unit, canonical
boot/audit session, and coherent none-or-audit-session attribution. Optional
source IP and TTY fields must be canonical strings when present; explicit null
does not match. Exact matches normally create no batch item, central Event,
Incident, notification, RuleSet handler, or public evidence. Protocol-valid
lifecycle-attribute near matches remain ordinary events with the established
protected replay and rich safe Event projection; malformed protocol envelopes,
required wire fields, identities, or timestamps still reject.

Sensor status adds signed-64 saturating informational `local_only_observed`,
`local_only_suppressed`, and `local_only_diagnostic_delivered` counters, nullable
maximum observed time, and fixed `{active,until,error}` diagnostic state.
Observed and diagnostic-delivered count original ingestion only; suppressed
counts default suppression or bounded stale-queue reconciliation once. Ordinary
rows are selected before diagnostic rows, and bounded persistent reconciliation
cannot starve ordinary/high-signal delivery. The root-only emergency sidecar is
host operations state—not an API, framework setting, desired policy field, or
protocol field—and queued diagnostics are suppressed in bounded passes once it
is inactive.

A compatibility delivery of the exact class produces only a published,
handler-none `MojoSecReceipt` with `feature_schema=local_only_receipt_v1` and no
new Event; its protected replay retains the complete validated sensor event.
First receipt is `accepted`, identical terminalized replay is `duplicate`, and
a same-ID/different-digest delivery is `rejected`. Terminalizing an identical
pending receipt preserves its Event pointer/row and every protected
replay/provenance field except that `feature_schema` becomes
`local_only_receipt_v1` and `disposition="local_only"` is added. Compatibility
handling never rewrites or deletes historical published receipts,
Events/Incidents, or their evidence, and normal retention is unchanged. Only the
`local_only_receipt_v1` compatibility schema is rejected as feedback and
explicit replay/shadow and filtered before learning candidate selection,
stratification, and quotas; legacy published `replay_features_v1` remains
eligible and untouched.

For count-one web Events, occurrence fields are direct evidence. For an
aggregate count greater than one, volatile fields exist only in
`last_occurrence_sample`, explicitly labeled `semantics: "last_occurrence"`
with `observed_at` from `last_seen`. Invalid individual public fields are
omitted fail-soft rather than poisoning receipt publication.

For ordinary Event publication, an `accepted` result also means any required
RuleSet handler dispatch has a durable receipt outbox job. Queue failure returns
`retry`. Request replay and a
five-minute central replayer recover pending/failed dispatch without duplicate
handler jobs.

## HTTP errors

Errors use a small unwrapped JSON object: `{"error": "reason"}`.

| Status | Meaning |
|---|---|
| `400` | Empty body, invalid content length, malformed JSON/gzip, or invalid batch schema/event. |
| `403` | Wrong auth type, missing protected permission, inactive group chain, or enrollment mismatch. |
| `413` | Compressed or decompressed body exceeds 512 KiB. |
| `415` | Content type is not `application/json`, or content encoding is not gzip/identity. |

Valid batches always return `200`; persistence or central-publication failures
are represented by per-event `retry` results rather than a batch-level 5xx.
Input the database can never store (NUL or lone-surrogate code points) and
receipts whose projected evidence was already pruned by retention return
per-event `rejected` — terminal, so the sensor frees the spool slot — never
`retry`.

## Privileged-operation evidence

Delivered `auth.sudo_command` Events remain rich: the exact bounded command,
actor, target, source IP when SSH attribution is proven, TTY, Audit session,
producer identity, unit/cgroup/SELinux context, semantic broker operation and
up to eight validated ancestors may be present. The wire and Event schemas are
still version 1 and all additions are optional, so older sensors and retained
receipts remain compatible.

Expected firewall automation may be absent from the central Event feed only
when the sensor proves healthy post-cutover jobman, sudo, root broker, and
target lineage plus matching begin/result receipts. The fixed classifier is
`jobman_firewall_operation_v1`. Missing, stale, conflicting, interactive,
direct, SSH-attributed, or audit-unhealthy activity is sent as an ordinary
Event. A diagnostic window uses the existing eventless local-only receipt
contract and does not create an Incident or enter learning/feedback metrics.

The browser-facing learning endpoints are separate from machine ingestion.
They require a human JWT with global security permissions; API keys are always
rejected and group/member grants never authorize this platform-wide surface.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| `POST` | `/api/incident/mojosec/feedback` | global `manage_security` or `security` | Append a disposition or immutable reversal |
| `POST` | `/api/incident/mojosec/proposal` | global `manage_security` or `security` | Create a draft/shadow/rejected immutable proposal revision |
| `POST` | `/api/incident/mojosec/replay` | global `manage_security` or `security` | Explicit offline evaluation of a draft/shadow proposal |
| `POST` | `/api/incident/mojosec/shadow` | global `manage_security` or `security` | Explicit offline evaluation of a shadow-labelled proposal |
| `GET` | `/api/incident/mojosec/metrics` | global `view_security` or `security` | Bounded detector receipt/disposition counts |
| `GET` | `/api/incident/mojosec/case` | global `view_security` or `security` | Paginated read-only shadow case list |
| `GET` | `/api/incident/mojosec/case/<id>` | global `view_security` or `security` | One case with at most 8 samples and 50 transitions |
| `GET` | `/api/incident/mojosec/case-metrics` | global `view_security` or `security` | Bounded aggregate shadow comparison metrics |

The case surfaces are platform/global security-admin reads. API keys and group
member grants do not authorize them, and there are no case mutation, approval,
recommendation, or execution endpoints. The authoritative Event/Incident feed
continues unchanged while cases are in shadow mode.

List parameters are `page` (default 1), `page_size` (maximum 100), and indexed
exact filters `state`, `urgency`, `sensor_kind`, and `resource_id`. State is
`observing` or `elevated`; urgency is `info`, `warning`, `high`, or `critical`;
sensor kind is `web` or `fim`. The response includes `has_more` rather than an
unbounded total scan. Detail returns bounded normalized samples and append-only
transition snapshots, never receipt replay JSON.

`GET /api/incident/mojosec/case-metrics?days=1&resource_id=vhost:17`
accepts 1–90 days and an optional exact resource. It returns case,
occurrence, receipt, projected-Event, distinct, overflow, urgency, and
compression totals. It never returns evidence arrays. Occurrences, receipts,
samples, and projected Events are separate counters; clients must not treat
one as an alias for another.

For Edge VHosts, `mojosec_policy` is the versioned opt-in configuration exposed
on the normal VHost graph. Registered impossible-path families are rejected by
the edge before an SPA/upstream response. The sensor supplies only the fixed
response class, derived `vhost:<id>` resource identity, and policy version.
Clients must not infer response content or compromise from status/length and
must not centrally fetch a suspicious response.

Feedback accepts exactly one subject: `receipt_id` or `manual_exemplar`.
Optional `incident_id` is linked context and must match the explicit published
receipt; the server never chooses an arbitrary receipt from an incident. A
manual exemplar is only
`{"kind": "web.probe", "count": 3, "severity": "high"}`-shaped; arbitrary
evidence is rejected. `disposition` is one of `confirmed_threat`,
`expected_administrative`, `benign_noise`, `operational_failure`, `unknown`, or
`missed_incomplete`. To correct a row, repeat the same subject and include
`reverses_id` plus a required note. Notes are untrusted and capped at 1,000
characters.

Proposal content is deliberately non-executable:

```json
{
  "summary": "Raise the aggregate threshold for repeated web probes",
  "status": "draft",
  "content": {
    "schema": "mojosec.policy-proposal.v1",
    "detectors": [{
      "kind": "web.probe",
      "decision": "flag",
      "minimum_count": 5,
      "minimum_severity": "warning"
    }]
  }
}
```

Create a revision by sending the prior row as `supersedes_id`. Only `draft`,
`shadow`, and `rejected` exist; no request can activate live policy. Content
cannot contain regex, URL, handler, job, action, or arbitrary detector names.
Only an unsuperseded leaf may be evaluated, and a rejected leaf cannot be
evaluated or revised.

Replay/shadow requests require `proposal_id` plus an explicit, duplicate-free
`receipt_ids` list containing 1–100 retained receipts. The evaluator uses
stored `replay_features_v1` only, canonicalizes IDs in ascending order, and
recomputes each stored event projection's canonical digest against the receipt
payload digest before use. Altered or incomplete evidence fails closed. It
ignores host-reported severity in favor of the server `KIND_POLICY` level
mapping and returns bounded aggregate metrics and digests. Digests bind the
proposal content, evaluator schema/version, and server policy-registry digest.
`shadow` is not live-event
evaluation and does not attach anything to ingestion. Neither operation can
create an Event/Incident, call a handler or LLM, send an alert, or ban an IP.
Metrics inspect a hard-capped newest candidate pool (at most 4,000 rows per
receipt/current-feedback plane), then cap and stratify the requested sample per
stable installation identity. A noisy installation can make the result smaller
than the requested limit. Results contain no customer group/tenant stamp and
are not fleet coverage or sensor health. All learning models are excluded from
generic AI/model queries and assistant context attachment, and there is no
MojoSec assistant/LLM learning tool in this prototype. Existing incident-triage
and live RuleSet assistant tools are unchanged.
