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
| journald | accepted SSH logins, failed SSH authentication, sudo commands/failures, non-SSH PAM session opens, systemd unit-failure declarations (PID 1), and kernel out-of-memory kills; the exact trusted `systemd-user` lifecycle tuple is local-status-only unless its root diagnostic override is active | routine PAM close chatter, ordinary and err-level daemon log lines, kernel non-OOM errors, and userspace text echoing failure/OOM phrases |
| structured nginx log | known exploit-path probes, 401/403 denials, and 5xx responses; bounded raw request target, referrer, and user agent in root-only sensor state and the protected central receipt | ordinary 2xx/3xx/404/499 traffic, User-Agent-only suspicion, bodies, cookies, authorization, arbitrary headers, and raw log lines |
| immutable tiered integrity | 60-second host/config FIM, six-hour boot/system-binary FIM, RPM verification, and system-Python package integrity under the packaged `al2023-web-v2` profile | application release trees, MojoSec private state, symlink traversal, file contents, or an implicit whole-disk watch |

Sudo evidence retains bounded command context in the root-owned sensor spool,
the protected central receipt, and the existing security-admin Event surface:
actor, target user, TTY, audit context, working directory, executable path, and
the exact accepted command. The central projection validates without rewriting:
`command` is at most 2,048 UTF-8 bytes, while `command_path` and `cwd` are at
most 512 bytes each; NUL, invalid UTF-8, non-string, empty, and oversized values
are omitted independently. Literal sensor truncation markers project with an
accepted value so a retained prefix is never presented as complete. If a
marker key is present with anything other than literal boolean `true`, its
paired value and marker are malformed and both are omitted. The full command
digest remains receipt-only, while `command_family` may be added from
a valid non-truncated executable path. Every such path gets a server-owned
known family or the literal `unknown`; a missing, invalid, or sensor-truncated
path gets no family. Secret-looking arguments are deliberately not redacted on
Events already gated by `view_security` or `security`.

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

RPM ownership is structural, not parsed from `rpm -qf` prose or inferred from a
generic exit status. One `/usr/bin/python3 -I` helper and one read-only RPM
transaction serve the complete tier. Startup proves the `rpm` binding,
`TransactionSet`, `RPMDBI_INSTFILENAMES`, a readable package header, and a real
installed-file index lookup, then records the RPM database cookie. Each bounded
exact-path request returns zero, one, or at most two installed-state NEVRAs:
zero keeps the file under ordinary SHA-256 coverage, one selects RPM
verification, and multiple or malformed results make the tier incomplete. A
same-path header with a removed or other non-installed file state is not an
owner. Helper failure, timeout, protocol/output bounds, unexpected stderr,
query exhaustion, or a changed database cookie retains the prior authoritative
baseline. There is no BASENAMES, PROVIDENAME, localized CLI, or per-file process
fallback.

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

MojoSec's own control state (`/etc/mojosec`, `/var/lib/mojosec`,
`/run/mojosec`, the Audit rollback record included) is owned by the
`mojo.deploy.mojosec converge` transaction, excluded from the integrity
profile, and never journal scope — this manifest lives under `/etc/mojosec`, so
journaling that tree would let a declaration pre-authorize edits to the
sensor's own trust anchors. A *caller-declared* control-state path is dropped
with a warning instead of failing the deploy, because the declaring script body
is one generation older than the validator reading it (item 2014); paths a
producer derives are still refused outright.

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

Producer coverage explains a whole deploy, not just its package paths: the
journal auto-annotates each declared path's immediate parent directory (so
deploy-owned directory metadata under `/etc/cron.d`, `/etc/logrotate.d`, and
`/etc/nginx/conf.d` matches instead of surfacing unexplained), and
`mojo.deploy.mojosec converge` plus the render command's nginx runtime
fragment install run under the journal on enrolled hosts with the release's
deployment identity (`--deployment-id` / `$MOJO_DEPLOY_ID`). Centrally,
trusted expected-change FIM coalesces into one `MojoSecCase` per sensor +
deployment identity per UTC day; on an installation enrolled in
`authoritative` mode those receipts are case-routed — accepted, retained, and
replayable, but projecting **no** per-receipt operator Event — while
unannotated protected changes stay immediate individual Events (see
`docs/django_developer/logging/incidents.md` for the cutover contract, ack
ownership, and rollback).

Deploy the producer-capable package before activating `al2023-web-v2`. During
that first stage the profile stays inactive while normal deploy, node setup,
and certificate operations prove the stable helper path. Then preview every
integrity tier and initialize only its exact digest. Rollback selects a
retained initialized digest with `baseline-rollback`; it never substitutes a
new first-scan baseline.

### Structured nginx input

#### Trusted per-VHost response evidence

Edge VHosts may opt into a versioned, closed MojoSec policy. An empty policy is
the default and preserves the existing renderer. The configured form is:

```json
{
  "version": 1,
  "impossible_path_families": ["wordpress", "secret_files"],
  "response_class": "spa_fallback"
}
```

The object has exactly those three keys. `version` is an integer from 1 through
65,535, and the family list contains at most four unique entries chosen from
`admin_tools`, `php_runtime`, `secret_files`, and `wordpress`. The response
class must describe the VHost's configured serving behavior:

| VHost shape | Required `response_class` |
|---|---|
| `api` | `reverse_proxy` |
| `site` with SPA fallback | `spa_fallback` |
| `site` without SPA fallback | `static_site` |
| `site_api` | `site_api` |
| `redirect` | `redirect` |

Unknown keys, mismatched classes, unknown or duplicate families, and invalid
versions stop the render. Configured impossible paths receive an edge-generated
404 before SPA fallback or an upstream can answer them. This is an opt-in
serving policy, not a centrally initiated fetch or a detector inference.

For configured VHosts, the nginx security stream adds three server-derived
fields:
`response_class`, `resource_id` (`vhost:<database id>`), and
`edge_policy_version`. An impossible-path match has the registered class
`impossible_path`. The detector accepts only these fixed classes and resource
shape; unconfigured VHosts leave the fields empty and the detector omits them.
Status and byte length never establish content identity. Existing
trusted-proxy resolution remains unchanged, and the stream still excludes
bodies, cookies, authorization, query-derived case samples, and arbitrary
headers.

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
Aggregation identity is per kind. `web.probe` and `web.denied` keep the client
and peer IP in the fingerprint alongside host, method, status, and path — the
address is the actor being screened (probe feeds `block_ip`), so interleaved
identities do not collapse into a misleading row. `web.error` is keyed by
method, status, host, upstream status, and tokenized path only: one server
fault hits every client at once, so one outage grows one aggregate row per
failure shape instead of one per client, while the client address remains in
the evidence as the latest-occurrence sample.

### Journald and FIM traversal

Journald collection never uses `journalctl --lines` tail semantics. It streams
forward from the committed `--after-cursor`, stopping at both a record and byte
ceiling, and commits only the last cursor it processed. Per-record parse or
detector failures increment the malformed count and do not abort the burst.

System events use kernel-owned anchors. `system.service_error` fires only for
PID 1-authored unit-failure declarations — the record must carry `_PID=1` and
`_UID=0`, and the message must match systemd's failure grammar (`<unit>: Failed
with result '<r>'.` and the older entered-failed-state forms), or carry
systemd's published unit-failed `MESSAGE_ID` with a validated `UNIT=` field as
wording-drift armor. The event names the failed unit itself (never
`init.scope`) and is fingerprinted by `(unit, failure_kind)` so repeats
collapse into one aggregate; the bounded message stays in evidence only.
`system.oom` fires only for `_TRANSPORT=kernel` OOM kill lines — one critical
event per kill — and a caller-controlled syslog identifier never opens the
system branch, extending the trusted-source doctrine below.

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
command, executable, digest, and attribution provenance. The central Event
always reports `attribution` as `audit_session`, `who`, or `none`: audit-session
promotion requires a valid address, sensor-shaped actor and boot-ID strings,
and an audit session; `who` promotion requires a valid address plus
sensor-shaped actor and TTY strings. Missing, malformed, or non-string proof
therefore stays explicit `none` and cannot promote a stray address.
The `who` subprocess runs in a fixed C locale with a two-second timeout and
strict 128 KiB/4,096-line streaming caps; timeout or overflow fails closed.

`auth.session_open` is not another SSH-login event. The initial trusted form is
the exact AL2023 `systemd-user` PAM tuple, and its informational level-2
evidence records target/opener/producer UIDs, producer PID/executable/unit,
boot/audit/loginuid and available TTY. It accepts a source only from the exact
audit-session map, never `who`. Each `auth.sudo_command` describes one sudo
invocation; commands subsequently entered inside `sudo -s` are not observed by
this journal detector.

That one complete `systemd-user` lifecycle tuple is server-owned
`local_only`. It requires literal `kind="auth.session_open"`,
`severity="info"`, `summary="PAM service session opened"`, and
`recommendation="none"`; a canonical lowercase 64-hex observation fingerprint
or wire event ID; observation `aggregate=false` or wire `count=1` with
`first_seen == last_seen == observed_at`; canonical target user and UID; root
opener and producer; target UID equal to audit login UID; a positive producer
PID; exact `(systemd)` comm, `/usr/lib/systemd/systemd` executable, and
`user@<uid>.service`; canonical boot/audit session; and either no attribution
with no source-IP field or exact audit-session attribution with a canonical IP.
An optional TTY must be a canonical string when present. Missing, extra,
malformed, boolean-as-integer, explicit-null optional, contradictory, `who`, or
otherwise near-match lifecycle attributes do not classify. Fail-open-to-
ordinary applies only after the protocol has accepted the event: a
protocol-valid lifecycle near match remains ordinary fleet evidence, while a
malformed envelope, required wire field, identity, or timestamp still rejects
under the unchanged protocol validator.

For an exact original observation, the sensor advances its journal cursor and
updates local-only metadata in the same SQLite transaction, without aggregate
or ordinary spooling. All three counters saturate at signed 64-bit maximum:
`local_only_observed` counts original ingestions only,
`local_only_diagnostic_delivered` counts only original observations actually
admitted to the diagnostic spool, and `local_only_suppressed` counts default
suppression or later stale-queue reconciliation exactly once.
`local_only_last_seen` is the maximum validated original observation time; a
reconciliation pass never substitutes its wall-clock time.

SQLite persists an internal delivery class for restart-safe diagnostic queue
handling. Before selection, each pass reconciles at most 256 queued diagnostic
rows and at most 256 migrated legacy rows. With an active override, matching
legacy rows become diagnostic candidates; without it, queued diagnostics and
matching legacy rows are suppressed. Indexed ordinary rows are always selected
first, so reconciliation or an active diagnostic backlog cannot starve later
ordinary/high-signal events.

For short diagnostics only, a root operator may create
`/etc/mojosec/local_only_diagnostic.json`. This is not desired/effective
configuration and does not change protocol v1. The file must be a root:root
regular file at exact mode `0600`, at most 512 bytes, opened with `O_NOFOLLOW`,
with no duplicate JSON keys, and contain exactly the three schema keys. Generate
a fresh 15-minute expiry and install a new regular file with exact metadata:

```bash
diagnostic_tmp=$(mktemp)
python3 - <<'PY' > "$diagnostic_tmp"
import datetime, json
until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)
print(json.dumps({
    "schema": "mojosec.local_only_diagnostic",
    "version": 1,
    "until": until.isoformat().replace("+00:00", "Z"),
}, separators=(",", ":")))
PY
sudo install --owner=root --group=root --mode=0600 \
  "$diagnostic_tmp" /etc/mojosec/local_only_diagnostic.json
rm -f "$diagnostic_tmp"
```

`until` must be timezone-aware and no later than one hour after both the file's
mtime and current time; an mtime more than five seconds in the future is unsafe.
Delivery is active only while current time is strictly before `until`.
Missing, expired, unsafe, malformed, and far-future files preserve suppression.
An unsupported integer version is ignored as inactive with an empty error.
The public status adds exactly the signed-64 informational counters
`local_only_observed`, `local_only_diagnostic_delivered`, and
`local_only_suppressed`, nullable `local_only_last_seen`, and fixed
`local_only_diagnostic.{active,until,error}`. It never exposes sidecar bytes.
Remove the sidecar after diagnosis; the next bounded sender reconciliation
suppresses queued diagnostic rows while continuing to service ordinary rows.

On POSIX platforms FIM opens every path component relative to an already-open
directory descriptor with `O_NOFOLLOW`; files are hashed through that same
descriptor and checked again afterward. Enumeration is streaming and bounded
by `max_entries` and `max_depth`. A symlink race, permission loss, unsupported
descriptor-relative platform, or bound overflow makes the scan incomplete:
the previous baseline remains authoritative and a critical overflow event is
queued. There is no pathname-based fallback. An incomplete fast scan is also
never handed to the rpm tier as its shared `/usr/local` traversal — the polling
pass falls back to the last complete fast baseline, and a baseline preview marks
the rpm tier incomplete (with a reason) rather than displaying package state
derived from a truncated walk.

## Commands and health

```bash
# Validate config and, when RPM integrity is enabled, probe the isolated binding/index.
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

`check` does not open the API-key credential. For an RPM-enabled profile it does
start the same bounded isolated helper and fails unless the system binding,
transaction, read-only database/header access, and installed-file index are
usable. `check_node` grades that command as deployment readiness. Nothing
installs or falls back to a CLI binding automatically. `once` and `run` validate
the API-key credential when there is a batch to send. A delivery failure during
`once` is written to stderr and the status snapshot, but does not make the
command exit nonzero. Treat the `delivery` object in `status` as the canary
result rather than relying only on the process exit code.

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
    event__isnull=False,
    replay_features__feature_schema="replay_features_v1",
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

`/run/mojosec/status.json` is atomically written as root:root mode `0640`. Its
exhaustive top-level field set is `schema`, `version`, `sensor_id`, `state`,
`updated_at`, `config`, `collectors`, `delivery`, optional `integrity`,
`expected_changes`, `spooled_events`, `pending_aggregates`, `dropped_capacity`,
`dropped_aggregate_capacity`, `aggregate_evicted_for_priority`,
`delivery_accepted`, `delivery_duplicate`, `delivery_rejected`,
`delivery_retry`, signed-64 `local_only_observed`,
`local_only_diagnostic_delivered`, and `local_only_suppressed`, nullable
`local_only_last_seen`, and fixed
`local_only_diagnostic.{active,until,error}`.
It contains no API key, endpoint, raw log record, database row, FIM digest, or
file content. `check_node` reads that projection with sudo; ordinary app users
do not receive collector/backlog timing.

## Durability and batching

Private state lives in root-owned mode-`0700` `/var/lib/mojosec`; it must not be
placed under the application-writable `/opt/api/var` tree. SQLite uses WAL mode
and `synchronous=FULL`.

State schema v3 contains the v2 `ssh_sessions` correlation table/expiry index
and adds the private `events.delivery_class` plus separate indexed paths for
due delivery and `(delivery_class, created, id)` reconciliation. Fresh rows are
`ordinary`; rows preserved by v1/v2 migration are `legacy` until the bounded
classifier reconciles them. Startup inspects the stored version before
mutation. Both v1-to-v3 and v2-to-v3 run in one exclusive transaction, create
or repair every column/index, and write schema version 3 last. Rollback is
retryable and preserves queued events, aggregates, FIM baselines, SSH-session
state, metadata, and cursors. A future version is rejected unchanged. Schema
v3 is intentionally not downgrade-compatible with binaries that only support
v1/v2; roll back application code only together with compatible private state.

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

### Central correlation of auth and host evidence

No sensor change was needed for central auth/host correlation — every field
it keys on already crosses the wire. When an installation's enrollment row
sets `include_host` (see the incidents documentation for the full contract),
the receiver correlates `auth.ssh_failure`/`auth.ssh_login` per exact source
IP + account, `auth.sudo_command` per actor/target, and
`system.service_error` per unit + failure kind — hourly windows, strictly
per node. Under `mode: "authoritative"` those four kinds stop projecting
per-receipt Events (the case owns the acknowledgement, exactly like the
web/FIM cutover); `system.oom` and `auth.sudo_failure` always keep their
immediate Events and merely contribute. An SSH failure burst followed by a
success from the same IP promotes the case to critical and pages through
`mojosec.case.promoted`; the sensor's `recommendation` field remains
advisory-only either way. Two sensor limitations are documented rather than
worked around: `auth.sudo_failure` carries no actor/source attribution (its
detector fingerprints bounded message text), and OOM evidence names no
victim process. Optionally, `require_registered_deployments` on the same
enrollment row makes trusted-deployment FIM routing additionally demand a
driver-side pre-registered deployment id — an identity the node's root
cannot mint — via `POST /api/incident/mojosec/deployment`.

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
canonical payload digest. Identical replay of an already terminalized identity
returns `duplicate` whether or not that receipt has an Event; reusing an ID for changed
evidence returns `rejected`. For ordinary events, a receipt is acknowledged
`accepted` only after its bounded Event projection has completed central publication and any required
exact-RuleSet handler has a durably queued outbox job. Incomplete publication
or queueing returns `retry`.

`rejected` is also the terminal answer for input the receiver's storage can
never hold and for work that can never succeed: a batch whose
`policy_revision` contains unstorable text (NUL or lone-surrogate code
points) rejects every event with one log line and zero database work; an
individual event containing unstorable text is rejected before persistence;
a value-domain storage error (`DataError`/`UnicodeEncodeError`) that slips
past the pre-scan maps to `rejected` instead of an endless retry; and a
pending receipt whose projected Event was pruned by retention is rejected
with reason "event evidence was pruned before publication". Deployed sensors
already treat `rejected` as terminal — the spool row is freed — so none of
this requires a sensor upgrade, and genuinely transient failures keep the
`retry` behavior unchanged.

After protocol and digest validation, the receiver applies the same exact
local-only classifier before Event construction. A new match becomes a
published, handler-none, eventless receipt with the non-learning
`local_only_receipt_v1` compatibility schema: first delivery is `accepted`,
then identical delivery is `duplicate`. If an older publisher already completed
the identity, its receipt/Event/Incident are historical evidence and remain
untouched. If the same digest is still pending, the receipt row is locked,
terminalized, and its existing nullable Event pointer/row, protected replay
fields, and original policy/protocol provenance are preserved without
publication or handler dispatch: every replay/provenance field survives except
that `feature_schema` is replaced with `local_only_receipt_v1` and
`disposition="local_only"` is added. New eventless receipts retain the complete
validated sensor event in protected replay. A different digest rejects. Only
the `local_only_receipt_v1` compatibility schema is excluded from feedback,
explicit replay/shadow, metric candidate selection, and quotas; historical
published `replay_features_v1` evidence remains eligible and unchanged.
Compatibility handling never rewrites or deletes a historical published
receipt, Event, Incident, or projected evidence row, and normal receipt/Event
retention remains unchanged.

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

Wire attributes remain in the receipt's protected `replay_features`, which is
absent from the default graph and denied to generic AI/model query tools. The
central Event contains a fixed title/detail plus a per-kind validated
projection. SSH, reliably attributed sudo, and known web kinds promote a
canonical source IP. Web evidence canonicalizes method, host, peer, status,
path, upstream status/timing, and retains only an HTTP(S) referrer origin plus
token-normalized safe path, and structured UA family/major/digest plus its
centrally scrubbed display. This settled safe projection is unchanged by the
local-only disposition. Aggregated volatile samples are
omitted instead of presenting the last request as the whole distribution.
Sudo Event evidence exposes the exact accepted `command`, `command_path`, and
`cwd`, plus true-only truncation markers and an optional additive
`command_family` derived from a complete executable path. It retains actor,
target user, TTY, boot ID, audit session, and explicit attribution. The command
digest stays receipt-only. This raw administrative command evidence is
deliberately available through the existing `view_security`/`security` Event
surface; a present non-boolean or false truncation marker invalidates its
paired field rather than silently claiming the value is complete. No public
endpoint or broader graph is added. For
`fim.change`, the sole optional
sensor annotation projected centrally is exact
`expected_change.{deployment_id,expires_at,operation_id,operation_kind,completed_at}`
after revalidation; raw FIM
attributes never project and the annotation never suppresses publication. Host severity is
preserved only as advisory evidence; the registry selects the effective level
and category. Sensor summaries, journal messages, and other non-allowlisted raw
strings do not enter LLM-visible Event metadata. The explicit exception is the
exact bounded sudo `command`, `command_path`, and `cwd`, which intentionally
enter the existing security-admin Event metadata described above.

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

Both receipt state machines carry a terminal `dead` value. The replay cron
enforces `MOJOSEC_HANDLER_MAX_ATTEMPTS` (default 100, roughly eight hours of
continuous failure): a published receipt whose `handler_attempts` reaches the
cap is swept to `handler_state=dead` and never re-dispatched or logged again.
The same cron recovers `queued` receipts whose dispatch job vanished (job
prune, expiry, or a Redis restart): any receipt still queued after
`MOJOSEC_HANDLER_QUEUED_STALE_SECONDS` (default 1800) is dispatched inline,
which is idempotency-safe against a late-running original job. It also sweeps
pending receipts whose Event FK was nulled by event retention (at least one
day old) to `publish_state=dead`. Dead-lettering an extreme outage is
recoverable by deliberate bulk surgery — reset `handler_state` to `failed`
for the affected rows and the cron resumes them.

Published receipt rows are retained for `MOJOSEC_RECEIPT_RETENTION_DAYS`
(default 45, minimum 7) and pruned daily; `dead` receipt rows age out on the
same retention clock. Live pending publication and incomplete handler-outbox
receipts are never removed by that retention job.

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

### Privileged-process provenance and firewall noise

MojoSec records selective Linux Audit execution breadcrumbs rather than every
process. The managed AL2023 policy captures root-EUID execution, execution by
the deployed application AUID, and the exact sudo executable path. Audit
`SYSCALL`, `EXECVE`, `PROCTITLE`, `CWD`, and `EOE` rows are assembled by boot
ID plus Audit serial across polls; EOE closes a compound and a two-second
timeout closes it as incomplete. `/proc` is immediate optional enrichment for
PID generation, parents, cgroup/unit, namespace, executable, command line, and
SELinux context. Audit remains durable truth: a short-lived process or ancestor
that has already left `/proc` does not poison a complete Audit edge. A live
`/proc` identity that conflicts with Audit, PID reuse, cycles, ordering
conflicts, gaps, loss, or stale health makes suppression ineligible. Only the
long-lived JobEngine anchor must still have a live verified PID generation.

The cron origin is not inferred from a fabricated same-session `crond` exec.
It requires both halves of the production AL2023 launch: the trusted root
CROND CMD row (`_COMM=crond`, `_EXE=/usr/sbin/crond`, `_CMDLINE=/usr/sbin/CROND
-n`) carrying the exact project-specific jobman command, and an earlier Audit
`USER_START` row whose bounded quoted PAM message exactly names the app account,
the authorized `pam_loginuid,pam_keyinit,pam_limits,pam_systemd` grantors,
crond executable, cron terminal and successful result. If journald also exposes
`AUDIT_FIELD_GRANTORS` (or the legacy underscored spelling), every exposed
spelling must agree exactly. They agree on boot,
Audit session, app login UID, session scope and crond SELinux domain; the CMD
row additionally proves the app GID and launch PID. Strict monotonic order then
joins audited bash → jobman → engine generations. Bash and jobman may be
successive execs of the same launch PID or a strict parent/child pair. The
bounded launch record survives polling and restart; a conflicting duplicate is
sticky and makes the session permanently ineligible for suppression.

The root-owned Audit health oneshot has only `CAP_AUDIT_CONTROL` and publishes
a root-only sidecar every five seconds. The main sensor's capability set is
unchanged. It validates boot, managed generation, rules digest, sequence,
freshness, loss, backlog, failure mode, and rate limit. Deployment accepts only
the exact AL2023 `task,never` seed or a complete prior Mojo generation after
hashing every rules source, the generated rules and the active rules. It keeps
an exact rollback record and restores it on convergence failure or downgrade.
Mode-off and package downgrade first stop and verify the sensor inactive.
Before an older package is allowed to start or Audit assets are consumed, the stable root helper
transactionally converts every held broker candidate to an ordinary spool
Event and verifies that no pending row remains. The writer stays stopped through
handoff; failure restores the prior service/assets only after safe rollback.
The capability probe also selects the command-line contract: a pre-feature
module receives only its historical `--mode` and `--criticality` flags, never
new provenance-generation arguments such as `--project-path`.
Health units/timer, broker sudoers/wrapper, stable helper and sidecar are
retired by one shared finalizer only after the old-module converge or the
module-absent fallback cleanup has fully succeeded. Every earlier failure keeps
those recovery assets intact.

Audit-health v1 is closed after publication. Its exact fields are `schema`,
`version`, `boot_id`, `generation`, `rules_sha256`, `sequence`, `enabled`,
`failure`, `rate_limit`, `backlog_limit`, `backlog`, `lost`, and `updated_at`.
The command boundary projects only the six recognized `auditctl -s` status
fields; `pid`, backlog wait-time telemetry, `loginuid_immutable`, and future
kernel fields are ignored there. They can never silently enlarge v1. By
contrast, an unknown or duplicate key already present in the JSON sidecar is
malformed. Missing fields, booleans, negative or oversized counters, and
non-finite timestamps also fail closed. Runtime `healthy` and `reason`
annotations remain internal; durable previous-health state selects the same
canonical publisher fields before the next sequence comparison.

Process nodes live locally for seven days (131,072 rows), incomplete compounds
for ten minutes (8,192), origin sessions for 30 days (4,096), health epochs for
128 samples, and root-owned firewall receipt payloads for seven days (32,768 and 32
MiB). Pending broker observations are capped at 4,096/32 MiB and wait at most
30 seconds. Payload pruning targets a 256 MiB provenance operating budget and
SQLite checkpoints target a 64 MiB WAL; these are operational pressure budgets,
not hard whole-database file ceilings. Capacity pressure fails open and is
reported in sensor counters. A
non-journal collector has no new Audit-health authority and therefore cannot
flush a pending proof candidate; only an explicit unhealthy journal bracket or
the ordinary expiry deadline does so.
central Event receives at most eight compact ancestors; the complete graph and
raw Audit records remain on the sensor.

Application firewall work no longer invokes raw iptables/ipset sudo commands.
It sends one strict semantic JSON request to exactly
`sudo -n -- /usr/local/sbin/mojo-firewall-broker`; sudoers authorizes that
empty-argument command only. The broker generates the operation ID, validates
the SUDO caller and the same-runner JobEngine context, constructs all argv and
restore input, and emits root-owned begin/result receipts. Requests are at most
16 MiB/250,000 canonical networks; restore is at most 24 MiB; address space is
256 MiB; scalar work has 15 seconds and bulk work 120 seconds. Output overflow
is failure (64 KiB, or an 8 MiB hard ceiling for semantic rules reads).

`jobman_firewall_operation_v1` is local-only only after healthy post-cutover
cron/jobman → sudo → broker → target lineage and exact receipt/PID-generation
agreement. SSH, TTY, IP attribution, direct legacy sudo, missing context,
timeouts, restarts, audit gaps, eviction, receipt disagreement, and every
incomplete proof retain the original rich sudo Event. Legacy direct grants
remain for one rollback generation but never qualify for suppression.
The JobEngine context prevents accidental cross-job attribution through the
normal API; it does not resist hostile Python already running in that process.

Sensor `recommendation` values (`none`, `review`, `block_ip`) are advice, not an
instruction. A compromised root node can forge its own observations, so the
central receiver must authenticate the installation, revalidate every bounded
field, deduplicate IDs, map event kinds to server-owned severity/category
policy, and allow action only through explicit central rules. No MojoSec host
code invokes the firewall or incident database directly.

The legacy public OSSEC endpoints are disabled when `OSSEC_SECRET` is unset or
empty. When enabled, the header is checked with a constant-time comparison.
