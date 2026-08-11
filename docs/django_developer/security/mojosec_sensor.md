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
| structured nginx log | known exploit-path probes, 401/403 denials, and 5xx responses; bounded raw request target, referrer, and user agent in root-only sensor state and the protected central receipt | ordinary 2xx/3xx/404/499 traffic, User-Agent-only suspicion, bodies, cookies, authorization, arbitrary headers, and raw log lines |
| immutable tiered integrity | 60-second host/config FIM, six-hour boot/system-binary FIM, RPM verification, and system-Python package integrity under the packaged `al2023-web-v2` profile | application release trees, MojoSec private state, symlink traversal, file contents, or an implicit whole-disk watch |

Sudo evidence retains bounded raw command context only in the root-owned sensor
spool and protected central receipt: actor, target user, TTY, audit context,
working directory, executable path, and command digest accompany the command.
The Event projection is closed rather than heuristically scrubbed: it exposes
only a strict server-owned command family (or `unknown`) and one constant
redaction marker. Raw command text, executable/path, command digest, argument
count, and per-token digests never enter Event metadata, titles, details,
ordinary logs, or AI/default graphs.

This catches common automated reconnaissance for WordPress, PHP, ASP/JSP,
`.env`, `.git`, phpMyAdmin, PHPUnit, actuator, Swagger/OpenAPI, CGI, and similar
surfaces. Modern crawler or AI-bot identity strings are not trustworthy enough
to create incidents by themselves. A crawler becomes interesting when its
behavior hits a protected/probe path or produces denials/errors; otherwise its
traffic stays in access analytics rather than the security feed.

The v1 scope does not inventory processes, listening sockets, or kernel policy.
Package coverage is deliberately limited to RPM verification and the isolated
system interpreter's approved `/usr`, `/usr/lib64`, and `/usr/local`
site-packages; project virtualenvs and application release trees are excluded.
AWS-native findings and application-level authentication signals
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
  "profile": "al2023-web-v2",
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
      "max_line_bytes": 262144
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

The recommended AL2023 policy selects the immutable `al2023-web-v2` profile by
name. Its fast tier covers `/etc` (excluding `/etc/mojosec`), exact root and
`ec2-user` persistence locations, cron/at/cloud-init scripts, local executables,
and `/usr/local/lib` including system Python site-packages. Its slow tier covers
`/boot`, `/usr/bin`, and `/usr/sbin`; RPM verification independently reports
strictly parsed package drift. Profile paths and bounds cannot be overridden by
desired policy: changing the graph requires a new packaged profile name and
digest. Legacy custom FIM targets remain supported only when no profile is
selected.

`al2023-web-v1` remains packaged for baseline identity and rollback, but must
not be used for a new AL2023 baseline. Its cloud-init script target descends
through `/var/lib/cloud/instance`, a mutable symlink that descriptor-safe
traversal refuses. V2 removes only that redundant descendant and retains
recursive script-content coverage through `/var/lib/cloud/instances`. It does
not follow or separately attest the mutable alias metadata. Existing v1 state
remains active until the operator explicitly selects v2, completes every
preview tier, and initializes the exact v2 digest; mismatch is visible digest
drift and never an implicit rebaseline.

The unit uses `ProtectHome=tmpfs` and exact read-only binds for the approved
root and `ec2-user` SSH, user-systemd, local-bin, AWS config, and shell startup
paths. Unrelated home content such as `/root/.cache` remains hidden; `check_node`
probes the live mount namespace. Private MojoSec state is always rejected, and
the public `status_path` must remain outside the private `state_dir`.

An optional expected-change manifest annotates a reported `fim.change`; it
never suppresses one. Producers use the root-owned stable helper at
`/usr/local/lib/mojosec/mojosec_changes.py`, declare exact destinations before
starting one child mutation, and complete or abort from that child's result.
System pip changes are derived from bounded installer output plus incoming and
installed wheel `RECORD` paths; ordinary deploy, node setup, and certificate
sync declare their exact systemd/cron/nginx/lineage destinations. Repeated
destinations are deduplicated before bounds are enforced. Caller-declared
changes retain the 4,096 unique-path ceiling. System-package paths are instead
derived from bounded pip reports and wheel records and use a 65,536-path
secondary corruption ceiling; root-only journal and manifest state fail closed
at 20 MiB. Approved roots remain mandatory for every path. A failed or aborted
producer leaves its observed changes unexplained.

The current root-owned 0600 envelope is
`{"schema":"mojosec.expected_changes","version":2,"entries":[...]}`. Version
2 adds bounded `operation_id`, `operation_kind`, `started_at`, and `completed_at`
to the v1 exact `path`, `change`, SHA-256, timezone-aware `expires_at`, and
`deployment_id` fields. Existing unexpired v1 entries remain readable during
rollout. Path, change, type-bound comparison digest, operation window, and live
expiry must all match. Content/link digests and comparison-only timestamps/file
identity stay in the private spool and are removed from delivered and replayed
events. FIM events are committed immediately and normally held for 120 seconds;
an exact path in an active operation may extend that hold to the coherent
20-minute ceiling (15-minute operation plus five-minute completion window).
Events are then delivered whether or not an annotation arrives. Malformed
manifests emit a visible `fim.expected_change_error`; they never block expiry
delivery.

Deploy the producer-capable package before activating `al2023-web-v2`. During
that first stage the profile stays inactive while normal deploy, node setup,
and certificate operations prove the stable helper path. Then preview every
integrity tier and initialize only its exact digest. Rollback selects a
retained initialized digest with `baseline-rollback`; it never substitutes a
new first-scan baseline.

### Structured nginx input

Each configured nginx path is a newline-delimited JSON log, not the ordinary
combined access log. A record needs a numeric `status` and a non-empty path.
The detector recognizes these field names:

| Meaning | Accepted fields |
|---|---|
| timestamp | `time`, `time_iso8601`, or `timestamp` |
| method | `method` or `request_method` |
| raw request target | `request_uri`, `uri`, or `path` |
| client IP | `remote_addr` or `source_ip` |
| direct peer IP | `peer_addr` or `realip_remote_addr` |
| duration in seconds | `request_time` |
| virtual host | `host` or `server_name` |
| upstream result/timing | `upstream_status`, `upstream_response_time` |
| approved diagnostic headers | `referrer`/`referer`, `user_agent` |

The generated stream also supplies `request_id`, `scheme`, `protocol`,
`tls_protocol`, `tls_cipher`, client/direct-peer/server ports,
`request_length`, `response_bytes`, `response_body_bytes`, upstream
connect/header/response times, and upstream response-length/received/sent byte
measurements. Ports and counters are range checked. Optional upstream values
accept at most eight entries; empty and `-` entries mean “not observed,”
including mixed retry chains.

The timestamp is optional but, when present, must be timezone-aware ISO-8601.
The dedicated root-only stream deliberately carries the bounded raw request
target, referrer, and user agent so the protected central receipt can support
incident reconstruction. It never logs bodies, cookies, authorization, or
arbitrary headers. Every detector kind has an explicit field allowlist and
priority order. UTF-8 byte caps, total encoded-attribute budget, truncation
markers, and full-value SHA-256 digests are applied before SQLite persistence;
escaped lone-surrogate or oversized Unicode input cannot stall the cursor.
The wire protocol permits at most 8 KiB of encoded attributes; the native
sensor reserves 512 bytes and emits at most 7,680 bytes. For web evidence the
raw request target is capped at 2,048 bytes and raw referrer/user-agent values
at 1,536 bytes each; a raw sudo command has the same 2,048-byte cap (its
working directory and executable path are each capped at 512 bytes). A
retained truncated value gains
`<field>_truncated: true` and `<field>_sha256`; lower-priority fields may be
omitted once the total budget is full. The generated nginx stream writes all
fields in the table above plus the identity, protocol, port, byte, and
upstream measurements just listed.
Both standard and Edge nginx write this protected ingress stream to the
root-precreated `/var/log/nginx/mojosec.json.log` at mode `0600`; nginx's root
master opens the descriptor before workers drop privilege. The Edge staged
unprivileged `nginx -t` copy disables access logs and the authoritative root
check validates the real path. Copytruncate rotations remain root:root `0600`.
The collector accepts lines up to a fixed 256 KiB derived from four default
8 KiB nginx request/header buffers, worst-case JSON escaping, and envelope
overhead. Larger lines fail closed; accepted fields still pass through the
smaller per-kind evidence budget before persistence/transmission.
High-entropy path segments still use a shared token marker for aggregation.
Every Event-visible IP, host, method, status, and path participates in the
fingerprint, so interleaved identities do not collapse into a misleading row.

### Journald and FIM traversal

Journald collection never uses `journalctl --lines` tail semantics. It streams
forward from the committed `--after-cursor`, stopping at both a record and byte
ceiling, and commits only the last cursor it processed. Per-record parse or
detector failures increment the malformed count and do not abort the burst.

Only successful Linux audit-transport `USER_START`/`USER_LOGIN` records for
the trusted legacy sshd executable or AL2023 split
`/usr/libexec/openssh/sshd-session`, root UID, and `terminal=ssh` establish an exact
`(boot_id, audit_session)` mapping. Accepted-looking user journal text cannot.
The entire poll is overlaid before detection, so sudo may correlate to an SSH
record later in the same poll. A match also requires the same actor and a
compatible TTY. Mappings persist in SQLite for at most 30 days and 4,096 rows.
Conflicting actor, TTY, or address for one key creates a sticky ambiguous
tombstone that cannot restabilize, including after restart.
Journal parsing likewise requires the exact root-owned legacy sshd tuple, the
exact split-sshd tuple, or an exact `/usr/bin/sudo`/`/usr/sbin/sudo` tuple with
the real canonical invoking UID and an optional `session-<id>.scope`; a
caller-controlled syslog identifier is never authoritative. When no exact
audit-session mapping is available, portable plain `/usr/bin/who` may attribute sudo
only for one unique, fresh (five-minute) exact actor-plus-TTY row; stale,
reused, or ambiguous rows produce no source attribution. Sudo evidence records
`attribution_provenance` as `audit_session`, `who`, or `none`; only the first
two may promote the correlated address to `Event.source_ip`. SSH and valid web
source addresses also populate that canonical Event field. Sudo evidence
includes bounded actor, target, TTY, audit identity, working directory, raw
command, executable, digest, and attribution provenance.
The `who` subprocess runs in a fixed C locale with a two-second timeout and
strict 128 KiB/4,096-line streaming caps; timeout or overflow fails closed.

`auth.session_open` is not another SSH-login event. The initial trusted form is
the exact AL2023 `systemd-user` PAM tuple, and its informational level-2
evidence records target/opener/producer UIDs, producer PID/executable/unit,
boot/audit/loginuid and available TTY. It accepts a source only from the exact
audit-session map, never `who`. Each `auth.sudo_command` describes one sudo
invocation; commands subsequently entered inside `sudo -s` are not observed by
this journal detector.

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

An immutable profile never silently trusts its first scan. Preview all three
tiers, then initialize only with the exact digest printed by that complete
preview:

```bash
(cd / && sudo /usr/bin/python3 -E -P -m mojo.mojosec \
  --config /etc/mojosec/config.json baseline-preview)
(cd / && sudo /usr/bin/python3 -E -P -m mojo.mojosec \
  --config /etc/mojosec/config.json baseline-initialize \
  --confirm-digest <exact-preview-digest> --reason initial-al2023-canary)
```

`check_node` requires the active profile digest plus initialized `fast`, `slow`,
and `rpm` baselines. Rollback is explicit and digest-confirmed with
`baseline-rollback --confirm-digest <retained-prior-digest>`; retained profile
history is never removed merely because another profile is activated.

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
root:root 0600 inode, archives remain root-only, and a new line is collected
without nginx reopen or a stalled cursor. Copy/truncate has a narrow inherent
writer race, so compare generated probe IDs/counts across the forced rotation
and investigate any unexplained gap. `maxsize 50M` is evaluated by logrotate's
timer/command, not continuously.
For `al2023-web-v2`, perform this on a disposable AL2023 node after its
producer-first rollout. Confirm the initialized `fast`, `slow`, and `rpm`
tiers, their 60-second/six-hour schedules, the system Python RPM/non-RPM
partition, and the `ProtectHome=tmpfs` exact-bind mount probe. Also create,
delete, and recreate one monitored home path across a service restart while
confirming unrelated home content remains unavailable to the sensor.
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

State schema v2 adds only the `ssh_sessions` correlation table and expiry
index. Startup inspects the stored version before mutation. The v1-to-v2
migration is one exclusive transaction, creates the table/index, and writes
the new version last; rollback is retryable and preserves events, aggregates,
FIM baselines, metadata, and cursors. A future version is rejected unchanged.

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

Wire attributes remain only in the receipt's protected `replay_features`, which
is absent from the default graph and denied to generic AI/model query tools.
The central Event contains a fixed title/detail plus a per-kind scrubbed
projection. SSH, reliably attributed sudo, and known web kinds promote a
canonical source IP. Web evidence canonicalizes method, host, peer, status,
path, upstream status/timing, and retains only an HTTP(S) referrer origin plus
structured UA family/major and its digest. Aggregated volatile samples are
omitted instead of presenting the last request as the whole distribution.
Sudo Event command evidence exposes only a strict server-owned command family
(or `unknown`) and a constant `<redacted>` marker; raw command, executable/path,
command digest, argument count, request target, referrer, and UA strings never
enter Event metadata, title, details, ordinary logs, or AI/default graphs. For
`fim.change`, the sole optional
sensor annotation projected centrally is exact
`expected_change.{deployment_id,expires_at,operation_id,operation_kind,completed_at}`
after revalidation; raw FIM
attributes never project and the annotation never suppresses publication. Host severity is
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
deliberately. All learning models are denied to generic AI/model-query tools
and assistant context attachment.

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
