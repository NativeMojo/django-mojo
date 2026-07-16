---
# id is assigned by /scope on pickup — leave it blank
id: DM-011
type: bug
title: IP storage fields assume IPv4 / non-null — IPv6 truncated, None-IP drops/crashes
priority: P2
effort: M
owner: backend
opened: 2026-06-30
depends_on: []
related: [DM-009, DM-010]
links: []
---

# IP storage fields assume IPv4 / non-null — IPv6 truncated, None-IP drops/crashes

## What & Why
Several model fields that store the client IP assume IPv4 and/or a non-null value. Now that the
resolvers (DM-009 HTTP, DM-010 WS) hand downstream code a clean normalized IPv6 string or `None`
(instead of a mangled, always-present string), those assumptions break three ways:
- **Native IPv6 is silently truncated** at the DB layer (CharField too short) — corrupting
  security/audit records and breaking geo lookups against the stored value.
- **A `None` IP** (reachable on a garbage/missing IP — rare behind `asgi.inc`, but possible)
  either **silently drops** a record (swallowed `IntegrityError`) or **crashes** a non-nullable insert.
- One field's **subnet computation is IPv6-broken** (it assumes a `.`).

Why now: these are the direct downstream consequence of the DM-009/010 resolver fixes — finishing
them closes the IP-handling work as a unit, and IPv6 client traffic is increasingly common.

## Acceptance Criteria
- [ ] Every CharField IP-storage column can hold a full IPv6 address without truncation (≥ 45 chars).
- [ ] A `None`/absent client IP no longer silently drops a record or crashes an insert — the record
      is stored with a null IP, and a `None` IP is logged once (visible, not silently swallowed).
- [ ] `GeoLocatedIP` subnet computation is IPv6-safe (no garbage, no crash) for IPv6 inputs.
- [ ] Regression tests cover: a native-IPv6 value round-trips through Event/Incident/Log without
      truncation; a `None` `request.ip` records a login event (null IP) instead of dropping it and
      doesn't crash `BouncerSignal`; `GeoLocatedIP` subnet for an IPv6 input is sane.
- [ ] Migrations regenerated via `bin/create_testproject`; full suite green.

## Repro — bugs only
1. **(truncation)** A client with a native IPv6 address (e.g. `2001:db8:85a3::8a2e:370:7334`, 28+ chars)
   triggers a security event / login / log. The stored `Event.source_ip` / `Incident.source_ip` /
   `Log.ip` is truncated to its first 16 (or 32) chars.
   - Expected: full IPv6 stored.
   - Actual: truncated/corrupted at the DB layer.
2. **(None drop)** `request.ip` is `None` (e.g. a proxy not setting `X-Real-IP`). On login,
   `UserLoginEvent.track()` raises on the non-nullable `ip_address`, caught by the broad `except` at
   `account/rest/user.py:637` → the login event is silently dropped.
   - Expected: event recorded (null IP), failure visible.
   - Actual: silently lost.
3. **(subnet)** A geo lookup for an IPv6 IP computes `subnet = ip_address[:ip_address.rfind('.')]` →
   `rfind('.')` returns -1 → `ip_address[:-1]` = garbage.
   - Expected: sane subnet, or subnet grouping skipped for IPv6.
   - Actual: corrupted subnet, broken subnet-match cache.

## Investigation
**Root cause — confidence: confirmed** (full inventory by reading the models).

Complete cluster — fields to fix:

| # | Field | file:line | Definition | Problem | Fix |
|---|---|---|---|---|---|
| 1 | `Event.source_ip` | `incident/models/event.py:42` | `CharField(max_length=16, null=True, db_index=True)` | too short for IPv6 | `max_length=45` |
| 2 | `Incident.source_ip` | `incident/models/incident.py:35` | `CharField(max_length=16, null=True, db_index=True)` | too short (sibling) | `max_length=45` |
| 3 | `Log.ip` | `logit/models/log.py:16` | `CharField(max_length=32, null=True)` | too short for IPv6 | `max_length=45` |
| 4 | `GeoLocatedIP.subnet` | `account/models/geolocated_ip.py:28` (+ logic at `:684`) | `CharField(max_length=16, null=True, db_index=True)` | too short AND `rfind('.')` garbles IPv6 | `max_length=45` + IPv6-safe subnet |
| 5 | `UserLoginEvent.ip_address` | `account/models/login_event.py:21` | `GenericIPAddressField` (non-nullable) | `None` → IntegrityError, swallowed at `user.py:637` → silent drop | `null=True, blank=True` (+ log `None` once) |
| 6 | `BouncerSignal.ip_address` | `account/models/bouncer_signal.py:39` | `GenericIPAddressField` (non-nullable, no default) | `None` → crash at pre-auth assessment | `null=True, blank=True` (or caller guards) |

Subnet logic (`geolocated_ip.py:684`): `subnet = ip_address[:ip_address.rfind('.')]` → for IPv6,
`rfind('.')` = -1 → `ip_address[:-1]` = garbage; used at `:688` (subnet_match filter) and `:696`
(copied to the new row). Fix: compute via `ipaddress` (network prefix), or skip subnet grouping for IPv6.

**Confirmed SAFE (no change):** `GeoLocatedIP.ip_address` (GenericIPAddressField, primary lookup —
Django's GenericIPAddressField holds up to 39 chars, enough for our normalized values, which collapse
`::ffff:`-mapped to IPv4); `UserDevice.last_ip`, `BouncerDevice.last_seen_ip`, `PublicMessage.ip_address`,
`ShortLinkClick.ip` (all nullable GenericIPAddressField); `UserDeviceLocation.ip_address` (non-nullable
but always-set via unique_together); `BotSignature.value` (CharField 512).

**Helpers / conventions:** `mojo/helpers/request.py` `normalize_ip` (public; already collapses mapped /
strips port) means `request.ip` reaching these fields is already normalized — so the CharField fields
only need length, not re-validation. `ipaddress` (imported in `geoip/__init__.py`) for the IPv6-safe
subnet. No IP-field length convention in `.claude/rules/models.md` — establish **45** as the standard
for CharField IP columns.

**Design notes for scope:**
- Minimal fix is `max_length=45` on the CharField IP columns (keep CharField). Converting them to
  `GenericIPAddressField` is a bigger change and adds validation that could reject existing rows —
  reject that for now.
- For None-handling (`UserLoginEvent`, `BouncerSignal`): making the field nullable is the minimal fix
  (records with a null IP instead of drop/crash). Scope should decide whether a null-IP `BouncerSignal`
  is meaningful or should instead be **skipped at the caller** (a bouncer signal with no IP may be
  useless), vs `UserLoginEvent` where recording with a null IP clearly beats dropping.
- `request.ip` is `None` only on misconfiguration (`asgi.inc` always sets `X-Real-IP`), so None-handling
  is defensive/low-frequency; the **IPv6 truncation is the higher-frequency real issue**.

**Migrations:** incident (`Event`, `Incident`), account (`UserLoginEvent`, `GeoLocatedIP`,
`BouncerSignal`), logit (`Log`) → `bin/create_testproject` regenerates. Widening a CharField max_length
and adding `null=True` are non-destructive migrations.

**Regression-test feasibility:** model-level testit tests (`@th.django_unit_test`): store a ~39-char
IPv6 in Event/Incident/Log and assert it round-trips un-truncated; call `UserLoginEvent.track` /
`BouncerSignal` create with a `None` IP and assert no crash + record stored (null IP); `GeoLocatedIP`
geolocate/subnet with an IPv6 input → assert subnet is sane / no crash. Setup must delete its own rows
first (long-lived DB) per `.claude/rules/testing.md`.

## Plan
### Goal
Make every IP-storage model field hold a full IPv6 address and tolerate a `None` IP — no
truncation, no silent drop, no pre-auth crash, no IPv6-broken subnet.

### Context — what exists
The resolver fixes (DM-009/010) now hand downstream code a normalized IPv6 string or `None`; six
fields assume IPv4/non-null. `mojo/helpers/request.py` `normalize_ip` already cleans the value (so
the CharFields need only length, not re-validation); `ipaddress` is available for the subnet fix.
**Confirmed-safe — do NOT touch:** `GeoLocatedIP.ip_address` (GenericIPAddressField, 39 chars is enough
for normalized values), `UserDevice.last_ip`, `BouncerDevice.last_seen_ip`, `PublicMessage.ip_address`,
`ShortLinkClick.ip`, `UserDeviceLocation.ip_address`, `BotSignature.value` (CharField 512).

The six fields to fix (defs confirmed by inventory):
- `incident/models/event.py:42` — `Event.source_ip = CharField(max_length=16, null=True, db_index=True)`
- `incident/models/incident.py:35` — `Incident.source_ip = CharField(max_length=16, null=True, db_index=True)`
- `logit/models/log.py:16` — `Log.ip = CharField(max_length=32, null=True, default=None)`
- `account/models/geolocated_ip.py:28` — `GeoLocatedIP.subnet = CharField(max_length=16, null=True, db_index=True)`; subnet computed at `:684`, used at `:688` (subnet_match) / `:696` (copied to new row)
- `account/models/login_event.py:21` — `UserLoginEvent.ip_address = GenericIPAddressField(db_index=True)` (non-nullable); writer `track()` (~`:123`); caller `account/rest/user.py:637-640` wraps in broad `except` → silent drop on None
- `account/models/bouncer_signal.py:39` — `BouncerSignal.ip_address = GenericIPAddressField(db_index=True)` (non-nullable, no default) → crash on None at pre-auth

### Changes — what to do
1. `incident/models/event.py:42` — `Event.source_ip` `max_length` 16 → **45**.
2. `incident/models/incident.py:35` — `Incident.source_ip` 16 → **45**.
3. `logit/models/log.py:16` — `Log.ip` 32 → **45**.
4. `account/models/geolocated_ip.py:28` — `GeoLocatedIP.subnet` 16 → **45**.
5. `account/models/geolocated_ip.py:684` — IPv6-safe subnet (keep IPv4 path; add IPv6 branch):
   ```python
   if ':' in ip_address:                 # IPv6 — rfind('.') doesn't apply
       try:
           subnet = str(ipaddress.ip_network(f"{ip_address}/64", strict=False).network_address)
       except ValueError:
           subnet = ip_address
   else:
       subnet = ip_address[:ip_address.rfind('.')]   # existing IPv4 /24-ish prefix, unchanged
   ```
   (add `import ipaddress` at the top of `geolocated_ip.py` if not already present.)
6. `account/models/login_event.py:21` — `UserLoginEvent.ip_address` → add `null=True, blank=True`; in
   `track()` add a `logit.warning` when the resolved IP is `None` (visibility, not silent).
7. `account/models/bouncer_signal.py:39` — `BouncerSignal.ip_address` → add `null=True, blank=True`.
   **Build must read the bouncer consumers first:** if any do string ops assuming a non-null
   `ip_address`, instead skip signal creation when the IP is `None` at the caller. Default: nullable.
8. `bin/create_testproject` — regenerate migrations (incident, account, logit).
9. `tests/` — regression tests (below). `CHANGELOG.md` — security/correctness entry.

### Design decisions
- **`max_length=45`, keep `CharField`** (don't convert to `GenericIPAddressField`) — minimal; no new
  validation that could reject existing rows. 45 covers every IP string form.
- **Preserve the IPv4 subnet format** (`rfind`), only add an IPv6 branch — switching IPv4 to
  `ipaddress` would change `"1.2.3"`→`"1.2.3.0"` and break `subnet_match` against existing rows.
- **Nullable for the two `GenericIPAddressField`s** — minimal fix so a `None` IP records (null) instead
  of dropping (`UserLoginEvent`) or crashing pre-auth (`BouncerSignal`).

### Edge cases & risks
- **Migrations are non-destructive** (widening `max_length` + adding `null=True` don't rewrite/lose
  data). `bin/create_testproject` regenerates incident/account/logit migrations.
- **No backfill of existing truncated rows** — only new writes are correct; historical repair is OUT
  of scope (confirmed with user).
- **`BouncerSignal` null:** build verifies consumers tolerate a null `ip_address`; if not, skip-at-caller
  instead of nullable (see change 7).
- `request.ip` is `None` only on misconfig (`asgi.inc` always sets `X-Real-IP`) — None fixes are
  defensive; IPv6 truncation is the real-traffic issue.

### Tests
testit model-level tests; setup deletes its own rows first (long-lived DB, per `.claude/rules/testing.md`).
- **Default-suite** (run in the standard baseline): `Log.ip` 39-char IPv6 round-trips un-truncated
  (`tests/test_logit/`); `UserLoginEvent.track` with a `None` IP → record stored (null), no crash,
  warning logged (`tests/test_account/`); `BouncerSignal` create with `None` IP → no crash
  (`tests/test_account/`); `GeoLocatedIP` subnet for an IPv6 input → sane `/64` prefix, no garbage/crash
  (`tests/test_account/` or `tests/test_geofence/`).
- **Opt-in module** (`--extra slow`): `Event.source_ip` / `Incident.source_ip` 39-char IPv6 round-trips
  (`tests/test_incident/`).
- Run: `bin/run_tests --agent -t <module>` per area; green default-suite baseline BEFORE editing
  (`.claude/rules/build-baseline.md`); **then `bin/run_tests --agent --full` once at close** to verify the
  `test_incident` regressions (justified — this item modifies incident models; user approved).

### Docs
`CHANGELOG.md` (IP fields now hold IPv6 + are null-safe). A line in `.claude/rules/models.md` / models doc
establishing **45** as the CharField-IP-column convention. Post-build docs-updater syncs both tracks.

### Open questions
None blocking. `BouncerSignal` nullable-vs-skip is decided at build after reading the consumers (default
nullable); `--full` at close = yes; no historical backfill — all confirmed with the user.

## Notes
- Build baseline (2026-06-30, `bin/run_tests --agent`, default suite, HEAD includes DM-009/010):
  **GREEN** — 2272 total, 2216 passed, 0 failed, 56 skipped (`testproject/var/test_failures.json`,
  `"failures": []`). Any failure after this change is attributable to DM-011.
- BouncerSignal nullable confirmed safe: no string-ops consumers of `signal.ip_address` (grep empty);
  created with `ip_address=request.ip` at `account/rest/bouncer/event.py:70`. → make nullable.
- Regression tests consolidated in `tests/test_models/` (default suite) so the Event/Incident IPv6
  cases run in the standard baseline (no `--full` dependency for the regression itself); `--full` still
  run at close to confirm the opt-in incident/security modules survive the schema change.
- `geolocate(..., auto_refresh=False)` is network-free (refresh gated at geolocated_ip.py:716) — used
  by the subnet test.

## Resolution
- closed: 2026-07-03
- branch: main
- files changed: .claude/rules/models.md,CHANGELOG.md,docs/django_developer/account/geoip.md,docs/django_developer/account/login_events.md,docs/django_developer/logging/incidents.md,docs/django_developer/logging/logit.md,mojo/__init__.py,mojo/apps/account/migrations/0045_alter_bouncersignal_ip_address_and_more.py,mojo/apps/account/models/bouncer_signal.py,mojo/apps/account/models/geolocated_ip.py,mojo/apps/account/models/login_event.py,mojo/apps/incident/migrations/0030_alter_event_source_ip_alter_incident_source_ip.py,mojo/apps/incident/models/event.py,mojo/apps/incident/models/incident.py,mojo/apps/logit/migrations/0009_alter_log_ip.py,mojo/apps/logit/models/log.py,planning/.next_id,planning/in_progress/DM-011-ip-storage-fields-assume-ipv4-non-null-ipv6-trunca.md,planning/inbox/geofence-hardening.md,pyproject.toml,tests/test_models/ip_field_storage.py
- tests added:
