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
run a handler. Host severity cannot raise the server-owned level. Source IP is
eligible only for centrally registered attack kinds and only after their
central aggregate threshold; successful login and web-error reports never
promote an address for action.

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

Feedback accepts exactly one of `receipt_id`, `incident_id`, or
`manual_exemplar`. A manual exemplar is only
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

Replay/shadow requests require `proposal_id` plus an explicit, duplicate-free
`receipt_ids` list containing 1–100 retained receipts. The evaluator uses
stored `replay_features_v1` only, canonicalizes IDs in ascending order, and
returns bounded aggregate metrics and digests. `shadow` is not live-event
evaluation and does not attach anything to ingestion. Neither operation can
create an Event/Incident, call a handler or LLM, send an alert, or ban an IP.
Metrics are bounded sample counts, not fleet coverage or sensor health.
