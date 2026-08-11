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
| `duplicate` | Remove; the same event was already published. |
| `rejected` | Remove; the evidence is permanently invalid or conflicts with the ID. |
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

Raw bounded request targets, referrers, user agents, and sudo command context
are retained only in the protected `MojoSecReceipt.replay_features` audit
record (`DENY_AI`, excluded from the default graph). Event metadata contains a
central allowlisted projection: queryless/token-normalized path, canonical
method/host/status/upstream values, HTTP(S) referrer origin plus safe path,
structured UA family/major/digest plus centrally scrubbed display, request ID,
protocol/TLS, ports, byte counts and upstream measurements, and a strict
server-owned sudo command family (or
`unknown`) plus one constant redaction marker. Raw executable/path, command
digest, argument count, generic arguments, and per-token digests never project.
The native nginx stream never collects bodies, cookies, the Authorization
header, or arbitrary headers. Bounded request targets, referrers, user agents,
and sudo commands can still contain untrusted sensitive text, but those raw
values remain in the protected receipt. Event receives only centrally
validated and scrubbed forms; malformed or non-string textual fields are
omitted rather than stringified.

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
two allow sudo's address to populate `Event.source_ip`. Stale, reused, or
ambiguous rows therefore remain unattributed.

The current AL2023 split-OpenSSH executable and non-root invoking sudo UID are
accepted only under exact kernel/journal provenance. `auth.session_open` is an
informational local PAM service session—not an SSH login—and can use only exact
audit-session attribution. A sudo event records one sudo invocation; it does
not claim to capture commands later typed inside `sudo -s`.

For count-one web Events, occurrence fields are direct evidence. For an
aggregate count greater than one, volatile fields exist only in
`last_occurrence_sample`, explicitly labeled `semantics: "last_occurrence"`
with `observed_at` from `last_seen`. Invalid individual public fields are
omitted fail-soft rather than poisoning receipt publication.

An `accepted` result also means any required RuleSet handler dispatch has a
durable receipt outbox job. Queue failure returns `retry`. Request replay and a
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

## Learning API

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
