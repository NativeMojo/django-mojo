# Default Incident Rules for All Event Sources

**Status**: Done
**Priority**: Medium
**Created**: 2026-03-28

## Problem

Only OSSEC events have default rules. Every other event source that creates incidents — failed logins, token abuse, system health alerts — has **no automatic response**. Incidents are created but sit in the queue until a human or the LLM notices them.

The LLM agent catches these as a fallback (unmatched high-level events default to LLM triage), but that's expensive and slow for patterns that are well-understood. Default rules should handle the obvious cases so the LLM focuses on the ambiguous ones.

## Current State

| Source | Category | Level | Has Rules? | Current Response |
|--------|----------|-------|-----------|-----------------|
| OSSEC bot/scanner | `ossec` | varies | **Yes** (4 rules) | Block + ignore noise |
| Bouncer block | `security:bouncer:block` | 8 | **Planned** | Block high-confidence bots |
| Bouncer honeypot | `security:bouncer:honeypot_post` | 9 | **Planned** | Block credential stuffers |
| Bouncer campaign | `security:bouncer:campaign` | 10 | **Planned** | Block + notify |
| Failed login (unknown user) | `login:unknown` | 8 | No | Incident sits unhandled |
| Invalid token | `invalid_token` | 8 | No | Noise — no rule needed |
| Expired token | `expired_token` | 8 | No | Noise — no rule needed |
| Bouncer token invalid | `security:bouncer:token_invalid` | 7 | No | Incident sits unhandled |
| Rate limit hit | `rate_limit:{key}` | 5 | No | Event only, no incident |
| MFA failure | `totp:login_failed` | 1 | No | Event only, no incident |
| Runner down | `system:health:runner` | 10 | No | Incident sits unhandled |
| TCP overload | `system:health:tcp` | 8 | No | Incident sits unhandled |
| Scheduler missing | `system:health:scheduler` | 10 | No | Incident sits unhandled |
| CPU/Memory/Disk | `system:health:cpu/memory/disk` | 5 | No | Event only, no incident |

## Recommended Default Rules

Three categories of response: **security enforcement**, **auth protection**, and **infrastructure alerting**.

### Security Enforcement

These are attack patterns — the right response is block the IP.

**Bouncer token abuse:**
```
Category: security:bouncer:token_invalid
Name: "Bouncer - Token Abuse"
Priority: 5
Bundle by: SOURCE_IP, 30 minutes
Handler: block://?ttl=1800&fleet_wide=1
Rules: level >= 7
```
Rationale: Token replay, IP mismatch, and expired token reuse are deliberate — not accidental. Block for 30 minutes.

### Auth Protection

Failed logins and credential stuffing are high-volume. The key distinction: single failures are noise, repeated failures from the same IP are an attack.

**Credential stuffing (unknown users):**
```
Category: login:unknown
Name: "Auth - Credential Stuffing"
Priority: 5
Bundle by: SOURCE_IP, 15 minutes
Handler: block://?ttl=1800&fleet_wide=1
Rules: level >= 8
```
Rationale: `login:unknown` (level 8) means someone is trying usernames that don't exist. The event dedup system will increment `dedup_count` for rapid-fire attempts. A single incident is enough signal to block — if the username doesn't exist, there's no legitimate reason to keep trying.

### Infrastructure Alerting

These are NOT attacks — don't block IPs. The right response is notify humans immediately.

**Runner down (critical):**
```
Category: system:health:runner
Name: "Health - Runner Down"
Priority: 1
Bundle by: HOSTNAME, 30 minutes
Handler: notify://perm@manage_security,ticket://?priority=9
Rules: level >= 10
```
Rationale: A dead job runner means async work is stalled. Needs immediate human attention. Bundle by hostname to avoid duplicate tickets per server.

**Scheduler missing (critical):**
```
Category: system:health:scheduler
Name: "Health - Scheduler Missing"
Priority: 1
Bundle by: NONE, 60 minutes
Handler: notify://perm@manage_security,ticket://?priority=9
Rules: level >= 10
```
Rationale: No scheduler means no cron jobs running — health checks themselves will stop. High priority ticket.

**TCP connection overload:**
```
Category: system:health:tcp
Name: "Health - TCP Connection Overload"
Priority: 5
Bundle by: HOSTNAME, 30 minutes
Handler: notify://perm@manage_security
Rules: level >= 8
```
Rationale: High TCP connections could mean connection leak, DDoS, or load spike. Notify but don't create a ticket — often self-resolving.

## What About Low-Level Events?

Events below `INCIDENT_LEVEL_THRESHOLD` (default 7) don't create incidents. These are tracked as events only:

| Category | Level | Why no rule needed |
|----------|-------|--------------------|
| `rate_limit:*` | 5 | High volume, handled by rate limiter itself. Events are audit trail. |
| `invalid_token` / `expired_token` | 8 | Noise — happens constantly with mobile apps, tab refreshes, stale sessions. Not worth blocking over. |
| `invalid_password` | 1 | Single wrong password is noise. Brute force patterns caught by bundling at higher levels. |
| `totp:login_failed` | 1 | Single MFA failure is a typo. Repeated failures are worth watching but low priority. |
| `security:bouncer:monitor` | 5 | Bouncer already allowed them through — monitoring only. |
| `system:health:cpu/memory/disk` | 5 | Spiky but usually self-resolving. Events provide trend data. |

**Future consideration:** A periodic LLM trend analysis cron could scan low-level event patterns and escalate when it detects anomalies (e.g., 500 rate_limit events from the same subnet in an hour). This is a separate feature, not a default rule.

## Implementation

### Option A: Extend `ensure_default_rules()`

Add all rules to the existing method. Simple but makes the method very long and mixes OSSEC-specific logic with generic rules.

### Option B: Category-specific `ensure_*_rules()` methods (recommended)

```python
@classmethod
def ensure_default_rules(cls):
    """Create all default rules across categories."""
    cls.ensure_ossec_rules()
    cls.ensure_bouncer_rules()
    cls.ensure_auth_rules()
    cls.ensure_health_rules()
```

Each method is self-contained and idempotent (uses `_create_ruleset` with `get_or_create`). New event sources add their own method.

### Invocation

Today, OSSEC rules are lazily created on first OSSEC alert. This doesn't work for auth/health rules — those should exist from the start.

Options:
1. **Migration** — create rules in a data migration. Guarantees they exist.
2. **App ready** — create in `AppConfig.ready()`. Runs on every startup.
3. **Management command** — `./manage.py ensure_incident_rules`. Explicit, CI-friendly.
4. **First event** — lazy creation on first `publish()` call per category.

**Recommendation:** Option 4 (lazy) for consistency with OSSEC pattern, plus option 3 (management command) for explicit setup in deployment scripts. The lazy approach means zero overhead until events actually start flowing.

## Acceptance Criteria

- [x] `ensure_auth_rules()` creates default rules for: login:unknown, security:bouncer:token_invalid
- [x] `ensure_health_rules()` creates default rules for: system:health:runner, system:health:scheduler, system:health:tcp
- [x] `ensure_bouncer_rules()` creates rules for bouncer events (from bouncer_admin_visibility request)
- [x] `ensure_default_rules()` calls all category-specific methods (ensure_ossec_rules, ensure_bouncer_rules, ensure_auth_rules, ensure_health_rules)
- [x] All rules use `get_or_create` (idempotent, safe to call multiple times)
- [x] Security rules use `block://` handler — infrastructure rules use `notify://` + `ticket://` (never block IPs for health events)
- [x] Bundle settings prevent duplicate incidents for same source
- [ ] Management command available for explicit rule seeding (future)
- [x] Lazy creation: OSSEC `_ensure_defaults` calls `ensure_default_rules()` (all categories), bouncer `_ensure_bouncer_defaults` on first assess, health `_ensure_health_defaults` on first health cron
- [ ] Docs updated with default rule reference (future)
