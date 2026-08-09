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
invalid bounds, symlinks, insecure endpoints, and unsupported versions fail
closed. The root service also requires the config and API-key credential to be
regular files with mode `0600` (or stricter) and root ownership.

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
      "max_lines": 2000,
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
    "max_aggregates": 10000
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
the sample unchanged.

## Commands and health

```bash
# Parse all fields and audit config ownership/mode.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json check

# One collection/delivery cycle; useful for a deployment canary.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json once

# Read the public health snapshot without opening the private SQLite database.
python -I -m mojo.mojosec --config /opt/api/var/mojosec.json status
```

`/run/mojosec/status.json` is atomically written as mode `0644` and contains
only sensor identity, collector freshness/errors, delivery counts, spool depth,
aggregation depth, and capacity-drop counters. It contains no API key, raw log
record, database row, FIM digest, or file content.

## Durability and batching

Private state lives in root-owned mode-`0700` `/var/lib/mojosec`; it must not be
placed under the application-writable `/opt/api/var` tree. SQLite uses WAL mode
and `synchronous=FULL`.

- A journal/nginx cursor advances in the same transaction that queues or
  aggregates the observations preceding it.
- A complete FIM baseline advances in the same transaction as its change
  events. An incomplete/overflow scan leaves the baseline untouched and emits
  one aggregatable overflow signal; the next complete scan reconciles it.
- Event IDs are deterministic for the sensor, detector fingerprint, and
  aggregation window. A retry sends the same IDs.
- Events stay committed until the receiver acknowledges each ID as accepted,
  duplicate, or permanently rejected. Missing or retry acknowledgements receive
  bounded exponential backoff.
- The spool and aggregation tables are capped. Low-priority events cannot use
  the configured high/critical reserve. When even the reserve is exhausted,
  the sensor records explicit capacity-drop counters rather than claiming
  unconditional delivery.

The wire format is `mojosec.batch` version 1, optionally gzip-compressed, over
HTTPS with `Authorization: apikey <per-installation-token>`. The checked-in
golden fixture under `tests/test_mojosec/golden/` is the compatibility contract
for sensor and receiver implementations.

## Trust boundary

Sensor `recommendation` values (`none`, `review`, `block_ip`) are advice, not an
instruction. A compromised root node can forge its own observations, so the
central receiver must authenticate the installation, revalidate every bounded
field, deduplicate IDs, map event kinds to server-owned severity/category
policy, and allow action only through explicit central rules. No MojoSec host
code invokes the firewall or incident database directly.
