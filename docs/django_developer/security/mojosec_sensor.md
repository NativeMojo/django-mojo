# MojoSec Host Sensor

MojoSec is django-mojo's settings-free security sensor for dedicated EC2 web
nodes. It reads a deliberately small set of host signals, turns them into a
versioned event contract, aggregates repetitive activity, and delivers bounded
batches to the central incident system. It never imports Django settings and it
never bans an address locally. The incident system remains the policy and
enforcement authority.

The Python package is `mojo.mojosec`. A deployed node invokes it as an installed
package from a root-owned working directory with safe-path mode:

```bash
(cd / && /usr/bin/python3 -E -P -m mojo.mojosec --config /etc/mojosec/config.json run)
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

## Configuration and enrollment boundary

There are three separate inputs. `/opt/api/var/mojosec.json` is the nonsecret,
fleet-managed desired policy. It may tune bounded collection, targeted FIM,
aggregation, and batching, but it is never read by the service. It cannot set
host identity, endpoint, credential/state/status/manifest paths, nginx path,
trusted proxy boundary, lifecycle mode/criticality, or disable core
journal/nginx monitoring. `/etc/mojosec/enrollment.json` is root:root 0600 and
owns those protected host boundaries. Root convergence validates both, merges
them with fixed paths, and atomically writes the only runtime file,
`/etc/mojosec/config.json`, as root:root 0600.

All JSON is strict: unknown/duplicate fields, non-finite numbers, invalid
bounds, insecure endpoints, and unsupported versions fail before operational
mutation. No-follow descriptor reads prevent pathname replacement. The endpoint
must be HTTPS without credentials/query/fragment and exactly
`/api/incident/mojosec/batch`; a trailing slash is rejected. Delivery ignores
proxy environment variables and refuses redirects.

Observe mode's privileged launcher requires `/usr/bin/python3` 3.11+ and uses
`-E -P` from root-owned `/`. This ignores Python environment injection and the current
directory without dropping AL2023's root-owned `/usr/local` site-packages.
AL2 nodes must provision Python 3.11+ before enrollment; legacy Python 3.10
nodes remain able to converge off during an ordinary framework upgrade.
`check_node` verifies the version, safe-path flags, package origin, and effective systemd working
directory/environment. `-I` and `-s` are not compatible with the AL2023
root-pip layout.

Root enrollment for a standard EC2 nginx node:

```json
{
  "version": 1,
  "sensor_id": "prod-web-i-0123456789abcdef0",
  "endpoint": "https://incident.example.com/api/incident/mojosec/batch",
  "mode": "observe",
  "criticality": "required",
  "nginx_plane": "standard",
  "trusted_proxy_cidrs": ["10.0.0.0/16"],
  "fim_allowed_roots": [
    "/opt/api",
    "/etc/nginx",
    "/etc/systemd/system",
    "/usr/local/bin"
  ]
}
```

For Edge, set `"nginx_plane":"edge"` and optionally a root-enrolled
`edge_log_dir`; it must match file-only `EDGE_LOG_DIR`, and the generated
collector path is exactly `<edge_log_dir>/mojosec.json.log`.

The app/fleet desired-policy source contains no endpoint, identity, or secret:

```json
{
  "version": 1,
  "policy_revision": "prod-2026-08-08",
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

FIM targets are operational policy, not universal defaults, and must remain
beneath a root-enrolled allowed root. `/home` is deliberately inaccessible
under the unit's `ProtectHome=true`; private MojoSec state is always rejected.
Keep the profile
small enough that every change is meaningful. The deployment should generate
the exact code/config/systemd/nginx paths for that project rather than copying
the sample unchanged. An enabled FIM collector requires at least one target;
set `collectors.fim.enabled` to `false` when the deployment has no approved
profile. The public `status_path` must remain outside the private `state_dir`.

An optional expected-change manifest annotates a reported `fim.change`; it
never suppresses one. The root-owned 0600 JSON envelope is
`{"schema":"mojosec.expected_changes","version":1,"entries":[...]}`. Each
entry must name an exact absolute `path`, `change` (`created`, `modified`, or
`deleted`), SHA-256 of the resulting file (the prior file for deletion),
timezone-aware `expires_at`, and bounded `deployment_id`. Path, change, digest,
and live expiry must all match. Missing, expired, or mismatched entries behave
like no expectation; malformed/insecure manifests fail that FIM poll visibly.

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
(cd / && sudo /usr/bin/python3 -E -P -m mojo.mojosec --config /etc/mojosec/config.json check)

# One collection/delivery cycle; useful for a deployment canary.
(cd / && sudo /usr/bin/python3 -E -P -m mojo.mojosec --config /etc/mojosec/config.json once)

# Audit bounded status without opening config, credential, or SQLite content.
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.check_node --section mojosec \
  --mojosec-mode observe --mojosec-sensor-id prod-web-i-0123456789abcdef0)
```

`check` does not open the API-key credential; `once` and `run` validate it when
there is a batch to send. A delivery failure during `once` is written to stderr
and the status snapshot, but does not make the command exit nonzero. Treat the
`delivery` object in `status` as the canary result rather than relying only on
the process exit code.

An observe-only canary should exercise the actual nginx and authenticated
receiver path without changing firewall policy. Set the persistent root
enrollment to observe/required before allowing another deploy. Record an exact
UTC start timestamp, then use the real canary vhost:

```bash
# Use the real canary vhost so the request traverses the active nginx graph.
curl -sS -o /dev/null -H 'Host: canary.example.com' \
  http://127.0.0.1/wp-login.php
# Use a canary-only application route deliberately returning 503.
curl -sS -o /dev/null -H 'Host: canary.example.com' \
  http://127.0.0.1/__mojosec_canary_503
sleep 12
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.check_node --section mojosec \
  --mojosec-mode observe --mojosec-sensor-id prod-web-i-0123456789abcdef0)
```

Then verify centrally that a published receipt for that sensor was created
after the request (not merely that the local spool accepted it):

```python
from mojo.apps.incident.models import MojoSecReceipt
MojoSecReceipt.objects.filter(
    sensor_id="prod-web-i-0123456789abcdef0",
    publish_state="published",
    created__gte=canary_started_at,
).order_by("-created").values("wire_event_id", "created").first()
```

Also require a benign SSH login/logout, harmless sudo command, and controlled
FIM create/modify/chmod/delete under the enrolled canary root. Force a receiver
503 outage for only the canary key, prove durable spool growth and drain after
recovery, restart the service and prove identity/cursor/baseline persistence,
then run targeted `logrotate -f`. Verify `copytruncate` preserves the active
root:root 0640 inode, archives remain root-only, and a new line is collected
without nginx reopen or a stalled cursor. Copy/truncate has a narrow inherent
writer race, so compare generated probe IDs/counts across the forced rotation
and investigate any unexplained gap. `maxsize 50M` is evaluated by logrotate's
timer/command, not continuously.
Gate on zero capacity drops, no sustained error, explainable noise, bounded
disk/FD/task growth, under 150 MiB memory/32 tasks, and under 5% of one CPU over
five idle minutes. Roll back persistently by reinstalling enrollment with mode
off, then run:

```bash
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec converge \
  --mode enrolled --criticality enrolled)
```

`/run/mojosec/status.json` is atomically written as root:root mode `0640` and contains
only sensor identity, collector freshness/errors, delivery counts, spool depth,
aggregation depth, capacity-drop counters, and desired/effective config hashes.
It contains no API key, endpoint, raw log record, database row, FIM digest, or
file content. `check_node` reads that projection with sudo; ordinary app users
do not receive collector/backlog timing.

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

Create that key only from a trusted server-side provisioning shell (choose the
platform's infrastructure authentication group; it is not a customer tenant):

```python
from mojo.apps.account.models import ApiKey, Group

sensor_id = "prod-web-i-0123456789abcdef0"
group = Group.objects.get(name="infrastructure")
key, token = ApiKey.create_for_group(
    group, f"mojosec:{sensor_id}", permissions={"mojosec_ingest": True})
metadata = dict(key.metadata or {})
protected = dict(metadata.get("protected") or {})
protected["mojosec"] = {
    "enabled": True, "sensor_id": sensor_id, "allowed_versions": [1],
}
metadata["protected"] = protected
key.metadata = metadata
key.save(update_fields=["metadata"])
print(token)  # transfer once through the approved secret channel
```

On the host, place the nonsecret desired policy at
`/opt/api/var/mojosec.json`, then provision protected inputs and converge:

```bash
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec install-enrollment) < enrollment.json
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec rotate-credential) < credential.txt
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.mojosec converge \
  --mode enrolled --criticality enrolled)
(cd / && sudo /usr/bin/python3 -E -P -m mojo.deploy.check_node --section mojosec \
  --mojosec-mode observe --mojosec-sensor-id prod-web-i-0123456789abcdef0)
```

The key-install command never accepts the secret in argv. Delete the transfer
file through the approved secret-handling process after provisioning. The
canary is incomplete until a deliberately generated high-signal request is
authenticated, accepted, and visible as a published `MojoSecReceipt` for this
exact sensor; a successful local service start alone proves no receiver path.

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
and validated scalar provenance only. For `fim.change`, the sole optional
sensor annotation projected centrally is exact
`expected_change.{deployment_id,expires_at}` after revalidation; raw FIM
attributes never project and the annotation never suppresses publication. A source IP is promoted only for a
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
