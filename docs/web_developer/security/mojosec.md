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

The key must carry `mojosec_ingest` and its protected server-side
`metadata.mojosec` profile must be enabled, name the same `sensor_id` as the
batch, and allow the submitted protocol version. Ordinary JWTs, group keys,
unenrolled keys, and sensor-ID mismatches receive `403`.

The body is the strict `mojosec.batch` v1 contract. It may be plain JSON or one
gzip member. Concatenated/trailing gzip data, duplicate JSON keys, unknown
schema fields, and oversized input are rejected. The authoritative example is
the checked-in `tests/test_mojosec/golden/batch_v1.json` fixture.

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
run a handler.
