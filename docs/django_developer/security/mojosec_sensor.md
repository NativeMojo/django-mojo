# MojoSec Host Sensor

MojoSec is django-mojo's settings-free security sensor for dedicated EC2 web
nodes. It reads a deliberately small set of host signals, turns them into a
versioned event contract, aggregates repetitive activity, and delivers bounded
batches to the central incident system. It never imports Django settings and it
never bans an address locally. The incident system remains the policy and
enforcement authority.

The Python package is `mojo.mojosec`. A deployed node invokes it as an installed
package so isolated mode does not trust the current directory:

```bash
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json run
```

## Deliberately narrow v1 signal set

| Collector | Retained | Intentionally omitted |
|---|---|---|
| journald | accepted SSH logins, failed SSH authentication, sudo commands/failures, non-SSH PAM session opens, systemd/kernel failures and OOM activity | routine PAM close chatter and ordinary service notices |
| structured nginx log | known exploit-path probes, 401/403 denials, and 5xx responses | ordinary 2xx/3xx/404/499 traffic, User-Agent-only suspicion, query strings, referrers, and raw log lines |
| targeted FIM | create/change/delete of explicit files or directory profiles; scan overflow | an implicit whole-disk watch, symlink traversal, file contents |

Sudo evidence retains the actor, target user, executable path, and a command
digest. Arguments are never persisted because command lines routinely contain
passwords, tokens, and other credentials.

This catches common automated reconnaissance for WordPress, PHP, ASP/JSP,
`.env`, `.git`, phpMyAdmin, PHPUnit, actuator, Swagger/OpenAPI, CGI, and similar
surfaces. Modern crawler or AI-bot identity strings are not trustworthy enough
to create incidents by themselves. A crawler becomes interesting when its
behavior hits a protected/probe path or produces denials/errors; otherwise its
traffic stays in access analytics rather than the security feed.

The v1 scope does not inventory processes, listening sockets, packages, or
kernel policy. AWS-native findings and application-level authentication signals
continue through their existing django-mojo paths.

## Configuration

The file is strict JSON: unknown fields, duplicate keys, non-finite numbers,
invalid bounds, insecure endpoints, and unsupported versions fail closed. The
config file and API-key credential must be regular files rather than symlinks,
with mode `0600` (or stricter); a root service also requires root ownership.
Config ownership/mode checks and the bounded JSON read use the same
`O_NOFOLLOW` descriptor, so replacing the pathname between checks cannot change
the bytes the root process parses.
The endpoint must use HTTPS, have no credentials, query, or fragment, and use
the `/api/incident/mojosec/batch` path (an optional trailing slash is accepted).
Delivery deliberately ignores proxy environment variables and refuses
redirects so credentials cannot be forwarded to a different host.

```json
{
  "version": 1,
  "sensor_id": "prod-web-i-0123456789abcdef0",
  "endpoint": "https://incident.example.com/api/incident/mojosec/batch",
  "policy_revision": "prod-2026-08-08",
  "state_dir": "/var/lib/mojosec",
  "status_path": "/run/mojosec/status.json",
  "credential_path": "/etc/mojosec/credential",
  "poll_seconds": 5,
  "collectors": {
    "journal": {
      "enabled": true,
      "max_records": 2000,
      "max_bytes_per_poll": 8388608,
      "max_record_bytes": 262144,
      "timeout_seconds": 10,
      "lookback_seconds": 300
    },
    "nginx": {
      "enabled": true,
      "paths": ["/var/log/nginx/mojosec.json.log"],
      "max_bytes_per_poll": 2097152,
      "max_line_bytes": 16384
    },
    "fim": {
      "enabled": true,
      "interval_seconds": 60,
      "max_entries": 20000,
      "max_file_bytes": 16777216,
      "max_depth": 64,
      "targets": [
        {"path": "/etc/nginx", "recursive": true, "exclude": ["*.swp"]},
        {"path": "/etc/systemd/system", "recursive": true},
        {"path": "/opt/api/app", "recursive": true, "exclude": ["var/**"]}
      ]
    }
  },
  "aggregation": {
    "window_seconds": 60,
    "flush_count": 25,
    "max_aggregates": 10000,
    "critical_reserve_aggregates": 1000
  },
  "delivery": {
    "batch_events": 100,
    "batch_bytes": 262144,
    "timeout_seconds": 15,
    "retry_min_seconds": 5,
    "retry_max_seconds": 300,
    "gzip": true,
    "max_spool_events": 50000,
    "critical_reserve_events": 1000
  }
}
```

FIM targets are operational policy, not universal defaults. Keep the profile
small enough that every change is meaningful. The deployment should generate
the exact code/config/systemd/nginx paths for that project rather than copying
the sample unchanged. An enabled FIM collector requires at least one target;
set `collectors.fim.enabled` to `false` when the deployment has no approved
profile. The public `status_path` must remain outside the private `state_dir`.

### Structured nginx input

Each configured nginx path is a newline-delimited JSON log, not the ordinary
combined access log. A record needs a numeric `status` and a non-empty path.
The detector recognizes these field names:

| Meaning | Accepted fields |
|---|---|
| timestamp | `time`, `time_iso8601`, or `timestamp` |
| method | `method` or `request_method` |
| path | `uri`, `request_uri`, or `path` |
| client IP | `remote_addr` or `source_ip` |
| direct peer IP | `peer_addr` or `realip_remote_addr` |
| duration in seconds | `request_time` |

The timestamp is optional but, when present, must be timezone-aware ISO-8601.
Query strings are removed before an event is stored. High-entropy, UUID,
long-numeric, email, and reset/verify-token path segments are replaced by a
short digest in evidence and one shared token marker in aggregation keys.
Referrers, user agents, and unrecognized fields are ignored. Keep each JSON
record on one complete line; oversized and malformed lines are counted and the
byte cursor advances past them so poison input cannot stall collection. A
normal partial trailing line is deferred until it is complete.

### Journald and FIM traversal

Journald collection never uses `journalctl --lines` tail semantics. It streams
forward from the committed `--after-cursor`, stopping at both a record and byte
ceiling, and commits only the last cursor it processed. Per-record parse or
detector failures increment the malformed count and do not abort the burst.

On POSIX platforms FIM opens every path component relative to an already-open
directory descriptor with `O_NOFOLLOW`; files are hashed through that same
descriptor and checked again afterward. Enumeration is streaming and bounded
by `max_entries` and `max_depth`. A symlink race, permission loss, unsupported
descriptor-relative platform, or bound overflow makes the scan incomplete:
the previous baseline remains authoritative and a critical overflow event is
queued. There is no pathname-based fallback.

## Commands and health

```bash
# Parse all fields and audit the config file's type, ownership, and mode.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json check

# One collection/delivery cycle; useful for a deployment canary.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json once

# Read the public health snapshot without opening the private SQLite database.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json status
```

`check` does not open the API-key credential; `once` and `run` validate it when
there is a batch to send. A delivery failure during `once` is written to stderr
and the status snapshot, but does not make the command exit nonzero. Treat the
`delivery` object in `status` as the canary result rather than relying only on
the process exit code.

`/run/mojosec/status.json` is atomically written as mode `0644` and contains
only sensor identity, collector freshness/errors, delivery counts, spool depth,
aggregation depth, and capacity-drop counters. It contains no API key, raw log
record, database row, FIM digest, or file content.

## Durability and batching

Private state lives in root-owned mode-`0700` `/var/lib/mojosec`; it must not be
placed under the application-writable `/opt/api/var` tree. SQLite uses WAL mode
and `synchronous=FULL`.

- A journal/nginx cursor advances in the same transaction that processes the
  observations preceding it. Capacity rejection records a drop counter in that
  transaction rather than retaining the rejected observation.
- A complete FIM baseline advances in the same transaction as its change
  events. An incomplete/overflow scan leaves the baseline untouched and emits
  one immediate critical overflow signal; the next complete scan reconciles it.
- Event IDs are deterministic for the sensor, detector fingerprint, and
  aggregation window. A retry sends the same IDs.
- Events stay committed until the receiver acknowledges each ID as accepted,
  duplicate, or permanently rejected. Missing or retry acknowledgements receive
  bounded exponential backoff.
- The spool and aggregation tables are capped. Low-priority events cannot use
  the configured high/critical reserves. At the aggregate hard cap, a high
  signal flushes the oldest low-priority aggregate to the spool before taking
  its slot. Critical host/FIM signals bypass aggregation. When even those
  controls are exhausted, the sensor records explicit capacity-drop counters
  rather than claiming unconditional delivery.

The wire format is `mojosec.batch` version 1, optionally gzip-compressed, over
HTTPS with `Authorization: apikey <per-installation-token>`. The checked-in
golden fixture under `tests/test_mojosec/golden/` is the request compatibility
contract for sensor and receiver implementations.

### Receiver enrollment and publication

Provision a separate API key for each installation. The key must carry the
protected `mojosec_ingest` permission and a server-owned enrollment profile:

```json
{
  "permissions": {"mojosec_ingest": true},
  "metadata": {
    "protected": {
      "mojosec": {
        "enabled": true,
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "allowed_versions": [1]
      }
    }
  }
}
```

`mojosec_ingest` is in the framework's API-key protection floor. A tenant
administrator cannot mint it; the platform provisioning path must create the
credential. The receiver requires the authenticated key's enrolled sensor ID
and version to match every batch. A key is not an enrollment wildcard.
Generic REST callers, including a confined `manage_group` administrator, cannot
change the protected profile. The API key's `group` is only the authentication
and administration container: neither the receipt nor the Event is stamped as
customer-tenant data. Infrastructure identity remains explicit through the
receipt's API-key/sensor fields and Event `metadata.mojosec.sensor_id` plus its
non-secret installation-key ID.
Use the in-place API-key rotation path for credential rotation so the key row,
receipt identity, and host history remain stable while the bearer secret changes.

The receiver validates compressed and decompressed sizes, strict UTF-8/JSON,
duplicate fields, the shared protocol schema, and every event before writing.
It persists a unique receipt for `(authenticated API key, event_id)` and the
canonical payload digest. Identical replay returns `duplicate`; reusing an ID for changed
evidence returns `rejected`. A receipt is acknowledged `accepted` only after
its bounded Event projection has completed central publication and any required
exact-RuleSet handler has a durably queued outbox job. Incomplete publication
or queueing returns `retry`.

`MojoSecReceipt` is an internal durable outbox/audit model, not writable browser
CRUD state. Its unique key is
`(api_key, wire_event_id)`. It retains the API key and projected Event links,
payload digest, protocol/policy provenance, publication state and attempts, a
bounded last error, selected RuleSet/Incident, handler outbox state, and the
replay features needed to distinguish an identical retry from conflicting
evidence. The default `RestMeta` graph omits
`payload_digest`, `last_error`, and `replay_features`; those fields are marked
sensitive, the whole model is denied to generic AI queries, and generic model
REST create/update/delete are disabled.

Wire attributes remain in the receipt's `replay_features`, which is denied to
generic AI/model query tools. The central Event contains a fixed title/detail
and validated scalar provenance only. A source IP is promoted only for a
server-owned per-kind registry after a minimum aggregate threshold. Successful
login and web-error evidence never promotes a source IP. Host severity is
preserved only as advisory evidence; the registry selects the effective level
and category. Sensor summaries, paths,
messages, commands, and other raw strings do not enter LLM-visible Event
metadata.

Events use exact categories such as `mojosec.web.probe` and
`mojosec.auth.ssh_login`. Only an active RuleSet for that exact category can
create an incident or dispatch a handler. Scope rules, catch-all rules, the
level threshold, and the default LLM fallback are disabled on this trusted
publication mode. This is why the sensor's `block_ip` recommendation remains
advisory: action exists only when an operator installs an exact central rule.

`Event.publish(..., dispatch_handlers=False)` records the selected RuleSet and
Incident on the receipt first. After that transaction commits, a dedicated
receipt dispatcher is queued with a stable receipt key. It invokes
`RuleSet.run_handler(..., strict=True)` with stable child idempotency keys, then
marks the receipt dispatched. Request replay and the five-minute
`replay_mojosec_handler_outbox` cron recover pending/failed work. An accepted
acknowledgement is never sent while required dispatch lacks a durable queue row.

Published receipt rows are retained for `MOJOSEC_RECEIPT_RETENTION_DAYS`
(default 45, minimum 7) and pruned daily. Pending publication and incomplete
handler-outbox receipts are never removed by that retention job.

## Human feedback and offline policy evaluation

MojoSec's learning loop is an operator-only, infrastructure-global control
plane. Every learning endpoint uses global `view_security` or
`manage_security`/`security` authorization and rejects API keys, including
keys that otherwise carry those permissions. Feedback is never tenant-owned:
it has no `group`, and sensor identity is snapshotted from the protected
`MojoSecReceipt.sensor_id` and installation API-key ID rather than an Event
group or a payload claim.

`MojoSecDetectorFeedback` is append-only. A disposition is exactly one of
`confirmed_threat`, `expected_administrative`, `benign_noise`,
`operational_failure`, `unknown`, or `missed_incomplete`. Exactly one subject
is required: an explicit published receipt exemplar or a manual exemplar
containing only allowlisted `kind`, `count`, and `severity` scalars. An
`incident_id` is optional linked context only and is accepted only when it
matches that explicit receipt; an incident is never sampled implicitly.
Corrections append a row through `reverses`; they never update the prior row.
The actor, subject, detector/category/level, enrolled sensor and policy-revision
digest are scalar snapshots. Nullable actor/receipt/incident links may later be
pruned without losing that audit record. Notes are untrusted text capped at
1,000 characters and the feedback model, notes, and manual evidence are denied
to generic AI/model-query tools.

Feedback, proposal, and evaluation rows reject instance updates/deletes and
default-manager queryset update/delete/bulk operations. Each carries a
canonical digest of its immutable fields, which services revalidate before
reversal, revision, evaluation, or metrics use. Evaluation deletion is exposed
only through the clamped retention service. A separate unique subject-head row
rejects ordinary update/delete and advances only through a transactional,
subject-matching compare-and-swap, so concurrent writers cannot fork or repoint
the current disposition. The named `maintenance_objects` managers on feedback
and proposal are only for database administration and migrations; ordinary
application code must never use them. Django's deletion collector may use an
unguarded base manager to apply the subject/author `SET_NULL` lifecycle.
Database flush/migration tooling may bypass these application guards
deliberately. All learning models are denied to generic AI/model-query tools.

`MojoSecPolicyProposal` is a separate immutable revision chain. Its only states
are `draft`, `shadow`, and `rejected`; there is intentionally no active state.
Content uses `mojosec.policy-proposal.v1` and accepts at most 24 known detector
kinds with fixed `flag`/`ignore`, integer count, and enumerated severity
predicates. Extra keys are rejected, so proposal content cannot carry code,
regex, URLs, jobs, handlers, or actions. It never becomes a `RuleSet`, and
manually authored live RuleSet/regex behavior is unchanged. The prototype has
no assistant/LLM learning tools: feedback, proposal creation, replay, and
shadow evaluation are human-only REST/service operations until a structural
server-side human-approval boundary exists. Existing incident-triage and live
RuleSet assistant tools are unchanged.

Replay and shadow are explicit offline operations. The operator must supply a
non-empty, duplicate-free set of at most 100 retained receipt IDs. IDs are
evaluated in canonical ascending order using only their stored
`replay_features_v1`. Before use, the evaluator recomputes the canonical digest
of the stored event projection and requires it to match the immutable receipt
payload digest; altered or incomplete evidence fails closed. No Event
properties, network calls, LLMs, jobs, handlers, incidents, alerts, or bans are
involved. Shadow additionally requires a
proposal revision already labelled `shadow`; only an unsuperseded leaf may be
evaluated, and a rejected leaf closes its lineage. Host-reported severity is
not an evaluator input: effective kind/count and severity/level are validated
and derived under the server's `KIND_POLICY`. Proposal content digests are
reproved before use. Persisted sample/result digests bind the evaluator
schema/version and a digest of that registry. The database retains only
aggregate per-kind metrics and sample/result digests, never per-receipt
decisions or copied evidence.
`MOJOSEC_LEARNING_EVALUATION_RETENTION_DAYS` controls these summaries (default
90, clamped to 30–3,650 days); human feedback and proposal revisions remain
audit history.

Detector metrics inspect at most four times the requested sample, capped at
4,000 indexed, newest candidates in each of the published-receipt and
current-feedback planes. They then return at most the requested 1,000 rows per
plane and cap each stable installation (`api_key_id` plus enrolled `sensor_id`)
to at most 100 and one tenth of the requested sample. A noisy installation can
therefore make a bounded run return fewer rows; this is a detector sample, not
a complete fleet census. Receipt candidates are canonically digest-verified.
Installation strata have no tenant/group stamp and report receipt/occurrence
and disposition counts only; they make no fleet coverage, liveness, or
sensor-health claim.

The receiver should return one result per event using the strict acknowledgement
schema (a `reason` string is optional):

```json
{
  "schema": "mojosec.ack",
  "version": 1,
  "results": [
    {"id": "<64-character-lowercase-sha256>", "status": "accepted"}
  ]
}
```

`accepted`, `duplicate`, and `rejected` are terminal and remove that event from
the spool. `retry` and omitted event IDs retain only those events with bounded
exponential backoff. A non-2xx response or malformed acknowledgement retains
the entire sent batch for retry.

## Trust boundary

Sensor `recommendation` values (`none`, `review`, `block_ip`) are advice, not an
instruction. A compromised root node can forge its own observations, so the
central receiver must authenticate the installation, revalidate every bounded
field, deduplicate IDs, map event kinds to server-owned severity/category
policy, and allow action only through explicit central rules. No MojoSec host
code invokes the firewall or incident database directly.

The legacy public OSSEC endpoints are disabled when `OSSEC_SECRET` is unset or
empty. When enabled, the header is checked with a constant-time comparison.
