<!-- generated from maestro item 1097 (Security board 37) — do not edit; the board item is the source of truth -->
# PLAN — Stop false-positive blocking: default rulesets + geoip threshold rewrite

Ian's directive: "Lets loosen the rules so we are not catching a user entering credentials
wrong, but instead catching highly probably bad actors. We also want to use our incident
system with rules so each system can tune its own rules."

Binding philosophy: open REST API platforms, anybody can call them. **Preventive
restriction is wrong; reactive detection of demonstrated bad actors is right and wanted.**
Automatic blocking is NOT the problem. **False positives are the problem.** The bar must be
"highly probable bad actor", never "user fumbled their credentials."

## IAN'S DECISIONS (2026-07-31)

1. **Scope: everything** — the ruleset fix AND the geoip rewrite.
2. **Existing deployments: leave them alone.** New defaults apply to fresh installs only.
   No force-update, no version bump, no data migration. **Instead: document the exact
   operator remediation** (REST calls to retune an existing ruleset) so an existing
   deployment can fix itself in two minutes.

## THE HEADLINE FINDING (verified against source)

Five default RuleSets firewall an IP **fleet-wide on a SINGLE event** because they omit
`trigger_count`. At `mojo/apps/incident/models/event.py:277`:

```python
if (created and (trigger_count is None or meets_threshold)) or transitioned_to_new:
    rule_set.run_handler(self, incident)
```

`trigger_count is None` → handler runs on the first matching event.

| RuleSet | file:line | Match | Handler | One event means |
|---|---|---|---|---|
| Auth - Credential Stuffing | `rule.py:478-490` | `login:unknown` lvl≥8 | `block://?ttl=1800&fleet_wide=1` | **One mistyped username → whole egress firewalled 30 min** |
| Auth - Bouncer Token Abuse | `rule.py:514-526` | `security:bouncer:token_invalid` lvl≥7 | `block://?ttl=1800&fleet_wide=1` | **One expired token (15-min TTL) → 30-min fleet-wide block. Fires in log-only mode.** |
| Bouncer - In-Session Freeze | `rule.py:673-685` | `...:session_freeze` lvl≥9 | `block://?ttl=86400` | One risk-90 session → 24-hour block |
| Bouncer - High Confidence Bot Block | `rule.py:656-668` | `risk_score >= 80` | `block://?ttl=3600` | One bouncer block → 1-hour block |
| OSSEC - Generic Web Errors | `rule.py:418-433` | `details` regex `^Web (Attack )?4(0[045])` | `block://?ttl=300` | One 404 → 5-min block |

`Auth - Password Brute Force` (`rule.py:496-510`) already does it correctly with
`trigger_count=5, trigger_window=15`. The mechanism exists and is used 20 lines away.

**Also:** `mojo/decorators/bouncer.py:85-89` calls `_report_token_event(... level=7 ...)`
**outside** the `if require:` guard, so a log-only deployment (documented as safe for
gradual rollout) still emits the event that triggers a fleet-wide block.

**Context correction:** `check_internal_threats()` still does NOT run automatically. It is
reachable only from `GeoLocatedIP.check_threats()` (`geolocated_ip.py:263`), whose only
callers are `on_action_threat_analysis` (`:741`) and `on_action_refresh` (`:738`). The
auto-refresh in `geolocate()` passes `check_threats=False` (`:802`). No cron. So the geoip
retune is preventive; the rulesets are what is hurting people today. Build order reflects
this.

---

## PHASE A — the live fix (build first, commit separately)

**A1. `mojo/apps/incident/models/rule.py` — add trigger gates to the five rulesets.**

- `ensure_auth_rules` "Auth - Credential Stuffing" (`:478-490`): `bundle_minutes=60`,
  `trigger_count=25`, `trigger_window=60`.
- `ensure_auth_rules` "Auth - Bouncer Token Abuse" (`:514-526`): `trigger_count=10`,
  `trigger_window=30`.
- `ensure_bouncer_rules` "Bouncer - In-Session Freeze" (`:673-685`): `trigger_count=3`,
  `trigger_window=60`.
- `ensure_bouncer_rules` "Bouncer - High Confidence Bot Block" (`:656-668`):
  `trigger_count=3`, `trigger_window=30`.
- `ensure_ossec_rules` "OSSEC - Generic Web Errors" (`:418-433`): `trigger_count=10`,
  `trigger_window=10`.

**Leave firing on the first event** (unambiguous attacks): honeypot (`:624-636`), campaign
(`:640-652`).

**Do NOT** add version bumping, `operator_modified`, or a data migration — Ian's decision 2.
`get_or_create(defaults=...)` means existing deployments keep their current rows. That is
intended.

**A2. `mojo/decorators/bouncer.py:85-89` — split the token-invalid level by cause, and
never block in log-only mode.**

- `expired`, `nonce_consumed`, `ip_mismatch` → **level 4**. These are benign lifecycle
  events: a 15-minute TTL expiring while a user reads the page, a double-submit, a cellular
  IP handoff. No user did anything wrong.
- `invalid_format`, bad signature, `page_type_mismatch`, `duid_mismatch` → **level 7**.
- **When `require` is False, cap the level at 4 regardless of cause.** A deployment that has
  not enabled enforcement must never firewall anyone. Treat this as a bug fix.

---

## PHASE B — the geoip rewrite (build second, commit separately)

**B1. Make thresholds runtime-tunable.** `mojo/helpers/geoip/threat_intel.py:10-33` reads
every constant at import via `settings.get_static`, which reads **file-based settings only**
(`mojo/helpers/settings/helper.py:180-198`) — never the DB-backed `Setting` store. So
nothing is tunable per deployment without a code edit and restart, which defeats "each
system can tune its own rules."

Replace the module-level constants with a lazy `_config()` helper using `settings.get(...)`
(DB-backed first, `helper.py:157-178`). **Keep the module attribute names bound to the same
defaults** so existing test patches still resolve, but read through `_config()` inside
`check_internal_threats`. **This breaks the current test patching strategy** —
`tests/test_geoip/test_threat_intel.py` patches module attributes and the package is
`serial` for that reason. Update both.

**B2. Invert the denylist to a two-tier allowlist.** The denylist is structurally wrong:
every NEW level-≥8 category silently becomes attack evidence. It has already failed twice
(`magic:unknown` at `rest/user.py:906`, and `rest_error` at `decorators/http.py:233` —
level **12**, fired by ANY unhandled 500, attributed to the client's IP).

```python
# Unambiguous — no legitimate user produces these.
GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES = [
    'security:bouncer:honeypot_post',
    'security:bouncer:campaign',
    'sensitive_field_probe',
]
GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD = 3

# Ambiguous — a real user CAN produce one. Needs volume AND breadth.
GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES = [
    'login:unknown', 'reset:unknown', 'magic:unknown',
    'totp:login_unknown', 'sms:login_unknown', 'token:unknown',
    'invalid_password',
]
GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD = 25
GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS = 10
```

Everything unnamed counts toward nothing — `rest_error`, `assistant:error:*`,
`system:health:*`, `traffic:*`, `rate_limit:*`, all permission-denied categories, and
critically `security:bouncer:block` / `security:bouncer:session_*`. **Excluding the
bouncer's own decisions is what breaks the self-confirming feedback loop** (a bouncer block
emits level-8 `security:bouncer:block` at `rest/bouncer/assess.py:190`, which today counts
toward `is_known_attacker`, which adds +40 to the next score, which makes the next block
more likely).

`invalid_password` moves UP into the suspect tier despite being level 5 — it is the
strongest credential-stuffing signal in the codebase and is currently invisible to the
attacker predicate because of the level-8 floor. For the suspect tier the category
allowlist replaces the level floor; keep `INTERNAL_ATTACKER_LEVEL_THRESHOLD = 8` as a
secondary floor for the confirmed tier only.

**Backward compatibility:** if a deployment has set the old
`GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES`, keep honoring it and emit a
`logit.warning` deprecation — nobody's config should silently invert.

**B3. Replace "count over 90 days" with a rate window.**

```python
GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS = 24   # replaces LOOKBACK_DAYS=90 for predicates
```

Keep `LOOKBACK_DAYS=90` as a **display-only** stat window for `total_events`,
`top_categories`, `last_seen_event` — those feed the admin UI and are useful over 90 days.
They just must stop driving a boolean.

Rationale: the predicate answers "is this address *currently* hostile", and the enforcement
it feeds is measured in minutes. 90 days answers "has anything ever gone wrong here", which
on a shared egress is always yes. 24h spans a slow stuffing run pacing under the rate
limits, and lets an address rehabilitate in a day.

**B4. Shared-egress suppressor.** `BouncerSignal` has `ip_address`, `muid`, `created` all
indexed (`models/bouncer_signal.py:33, 39, 52`), and `muid` is a **server-set HttpOnly
cookie** (`mojo/middleware/mojo.py:47-53`) present pre-auth — JS cannot forge it.

```python
GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES = 25
```

If `COUNT(DISTINCT muid)` for the IP in the window exceeds this, suppress the **suspect**
path only — this address fronts many independent browsers, i.e. a NAT. **Never suppress the
confirmed path.** If the query returns zero rows (deployment doesn't run the bouncer JS),
skip the suppressor entirely rather than defaulting either way. Wrap in its own try/except
so a missing account app never breaks the check.

Hardening: count only muids whose `BouncerDevice.first_seen` predates the window, so an
attacker cycling fresh muids on one IP cannot fake a NAT.

**B5. Redefine `is_known_abuser`.** Current definition (`≥10 events, mean level in [4,8),
90 days`) is not a detector — it counts "has this IP ever used the API." Every
`rate_limit:*` event is already deduped to one per key+IP per 60s in Redis
(`decorators/limits.py:38`), so **each event ≈ one minute spent over a limit**.

```python
GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX = 'rate_limit:'
GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD = 60   # in the same 24h window
```

60 events = an hour of the day over limits. Drop `avg_level` from the predicate entirely —
averaging severity across unrelated categories is not a meaningful statistic.

Note this is a semantic change to a federated field (`geolocated_ip.py:139` ships
`is_known_abuser` in the `federation` graph). Downstream consumers get a flag that means
something; say so in the docs.

**B6. Indexes.** `Event` has **no `Meta` class at all** — only single-column btrees. With
just the `source_ip` index, Postgres reads all rows for that IP across all time and then
filters on `created`; the window shortens the result but not the scan.

- `mojo/apps/incident/models/event.py`: add
  `class Meta: indexes = [models.Index(fields=['source_ip', 'created'])]`
- `mojo/apps/account/models/bouncer_signal.py`: add
  `models.Index(fields=['ip_address', 'created'])`

Both additive, index-only. **Then run `bin/create_testproject`** (required by
`.claude/rules/core.md` after any model change), then the suite.

**B7. Event properties for rule matching.** `Rule.check_rule` (`rule.py:834-863`) falls
through to `getattr(event, field_name)`, so **any property on `Event` becomes a matchable
rule field**. Add as lazily-computed, memoized properties on `mojo/apps/incident/models/event.py`:

| Property | Returns |
|---|---|
| `ip_recent_attack_events` | count of confirmed+suspect-category events for `self.source_ip` in the window |
| `ip_recent_distinct_targets` | `COUNT(DISTINCT model_id)` where `model_name='account.User'`, same filter |
| `ip_recent_distinct_devices` | `COUNT(DISTINCT muid)` from `BouncerSignal` for this IP in the window |

Memoize on `self._ip_stats` — `check_rule` refuses `_`-prefixed field names (`:849-850`), so
the cache is not itself addressable. `check_by_category` (`:791-794`) iterates every ruleset
in the category, so a property can be asked for several times per event; memoization is
required, not optional.

**B8. Ship NO default ruleset using B7's fields.** Document them as opt-in. Each is a DB
aggregate on the event path; a default rule referencing them would put that cost on every
event for every deployment.

**B9. Decay.** Today `update_threat_from_incident` (`geolocated_ip.py:424-425`) and
`block()` (`:473-478`) only ratchet up, and nothing recomputes — a single level-8 event
stamps an IP `medium` forever. Add `recheck_active_threats` to
`mojo/apps/incident/cronjobs.py` (daily) that re-runs `check_threats()` for `GeoLocatedIP`
rows with `last_seen` inside the window and a non-null `threat_level`, letting a recomputed
lower level **replace** the stored one — for non-`mojo`-provider records only. Leave the
`provider == 'mojo'` never-downgrade branch (`geolocated_ip.py:307-312`) untouched; that is
the federation contract.

**Note this is also what finally makes `is_known_attacker` run at all.** Combined with a
retune whose thresholds have never met production traffic, that argues for B10.

**B10. Dry-run flag.**

```python
GEOLOCATION_INTERNAL_THREAT_DRY_RUN = False
```

When true, `check_internal_threats` computes the verdict and logs it via `logit.info`
(including the counts that drove it) but returns `False, False`. Lets a deployment watch a
week of real traffic before anything can act on it. Default `False` so the retune is live
by default — but the docs must show the flag prominently for anyone upgrading a busy
deployment.

---

## Testing plan

Conventions: `.claude/rules/testing.md` and `docs/django_developer/testit/Overview.md`.
`from testit import helpers as th`, `@th.django_unit_test()`, `def test_x(opts):`, imports
inside the test body, every assert carries a descriptive message, setup deletes before it
creates.

**Extend `tests/test_geoip/test_threat_intel.py`** (already `serial`, already offline;
`_seed_events` at `:48` already delete-then-creates). It needs a `created=` parameter —
`Event.created` is `auto_now_add`, so placing events in time requires a follow-up
`.update(created=...)`.

- Window boundary: 30 suspect events at 25h old → attacker False; same 30 at 1h old with 10
  distinct `model_id` → True.
- **Allowlist fails closed (the actual bug):** 50 `rest_error` events at level 12 → False.
  Same for `assistant:error:unhandled` and realtime `auth`.
- Confirmed path: 3 `security:bouncer:honeypot_post` → True, not suppressed by device count.
- Breadth gate: 30 suspect events all against one `model_id` → False.
- Suppressor: 30 suspect events, 10 targets, 50 distinct `BouncerSignal.muid` → False.
  Zero BouncerSignal rows → suppressor skipped, still True.
- Feedback loop: 20 `security:bouncer:block` events at level 8 → False.
- Abuser: 70 `rate_limit:login` events in 24h → True; 70 `user_permission_denied` at level 4
  → False (this is the current false positive).
- Deprecation: old `GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES` still honored.
- Dry-run: verdict True by counts, but returns False and logs.

**Extend `tests/test_incident/test_default_rules.py`** for Phase A (package is
`requires_extra: ["slow"]`, so opt-in): assert each retuned ruleset has the expected
`trigger_count`/`trigger_window`; assert an event below the trigger leaves the incident at
`status='pending'` and fires no handler.

**Add a default-suite test for A2** asserting the emitted level for `expired` vs a bad
signature, and that log-only mode caps at 4.

Per `.claude/rules/git.md`: one test run at a time.

## Doc plan (NO CHANGELOG — the file is frozen)

- `docs/django_developer/account/geoip.md` — rewrite "Threat Intelligence" (`:350-400`) for
  the two-tier allowlist, hours window, breadth test, device suppressor, dry-run; update the
  Settings table (`:488-493`) with new names + the deprecation note; document the decay cron.
- `docs/django_developer/helpers/settings_reference.md:284-288` — replace/extend the
  `GEOLOCATION_INTERNAL_*` entries.
- `docs/django_developer/logging/incidents.md` — document the new Event properties and how
  to write a rate rule against them ("Rule Engine" `:438`, "Thresholds" `:493`). **Add an
  "auth and bouncer default rulesets" section** — only the OSSEC defaults are documented
  today (`:702`), so the rules that do the actual blocking are undocumented. Update
  "Integration with GeoLocatedIP" (`:743`).
- `docs/web_developer/logging/incidents.md` — "RuleSet Fields" (`:328`): document
  `trigger_count` / `trigger_window` / `retrigger_every` / `is_active` and the
  `/api/incident/event/ruleset` + `.../rule` CRUD endpoints with the
  `manage_security`|`security` perm.
- **REQUIRED by Ian's decision 2 — operator remediation.** Because existing deployments keep
  their current rulesets, add an explicit section (django track, incidents doc) titled
  something like "Retuning an already-bootstrapped ruleset", giving the exact REST calls to
  set `trigger_count`/`trigger_window` on an existing `RuleSet`, and naming the five
  rulesets that ship without a trigger gate in deployments bootstrapped before this change.
  This is the mitigation for not force-updating; it must be easy to find.

## Risks

- **Too loose to catch a patient attacker.** 25-in-24h with 10 distinct targets misses a
  low-and-slow stuffer. The per-account sliding window (`limits.py:325`, 10 attempts/900s
  keyed on `user.pk`) is unaffected by IP rotation and is the correct control there — do not
  weaken it.
- **Allowlist rot.** A new attack category next quarter counts for nothing until someone
  adds it. Mitigate with prefix matching and a doc note. Accepted deliberately in exchange
  for the failure direction.
- **Device suppressor is spoofable and often absent.** Mitigated by the `first_seen`
  hardening; unavailable entirely on deployments not running the bouncer JS.
- **`trigger_count` windows are anchor-based, not sliding** (`event.py:423-424` filters on
  `incident.created`). Events straddling a bucket boundary may never reach N. Biases toward
  missing, which is the safe direction.
- **B9 makes `is_known_attacker` run for the first time ever**, with thresholds that have
  never met production traffic. B10's dry-run is the mitigation.
- **Widening what counts (adding `invalid_password`) increases scan volume.** Mitigated by
  B6; without that index B2 is a performance regression on busy IPs.
- **Test fragility.** `tests/test_geoip` is `serial` because it patches process-global module
  attributes. B1 changes how those are read; if patching is not updated correctly the tests
  can pass while testing nothing. **Every new test must be verified to fail at HEAD.**
