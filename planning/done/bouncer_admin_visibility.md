# Bouncer Admin Visibility & Metrics

**Status**: Done
**Priority**: Medium
**Created**: 2026-03-28

## Problem

The bouncer system has a rich detection and scoring pipeline, but admins building a security dashboard have no way to know it exists or use it. Two gaps:

### 1. No documentation for admin-facing APIs

Three REST endpoints exist but are undocumented:

| Endpoint | Model | What it provides |
|----------|-------|-----------------|
| `/api/account/bouncer/device` | `BouncerDevice` | Device reputation: risk tier, event/block counts, linked identities, fingerprints |
| `/api/account/bouncer/signal` | `BouncerSignal` | Read-only audit trail: every assessment with raw signals, server signals, triggered signals, decisions, scores |
| `/api/account/bouncer/signature` | `BotSignature` | CRUD for bot signatures: auto-learned and manual, hit/block counts, expiration |

The current `docs/web_developer/account/bouncer.md` only covers client-side integration (assess endpoint, token flow, pass cookies). An admin building a security dashboard doesn't know these APIs exist.

### 2. No bouncer-specific metrics

Zero `metrics.record()` calls exist in the bouncer code. An admin can't build charts for:

- "Blocks per hour — are bot attacks increasing?"
- "How many pre-screen cache hits vs full scoring blocks?"
- "Honeypot catch rate"
- "Signature learning — how many new signatures this week?"
- "Campaign detection rate"

The incident system captures bouncer events (block, campaign, honeypot), but these are mixed with all other incident types. No way to query bouncer-specific trends via the metrics API.

### What already works

Bouncer events DO flow into the incident system correctly:

| Category | Level | Creates Incident? |
|----------|-------|-------------------|
| `security:bouncer:block` | 8 | Yes (above threshold 7) |
| `security:bouncer:campaign` | 10 | Yes (critical) |
| `security:bouncer:honeypot_post` | 9 | Yes |
| `security:bouncer:token_invalid` | 7 | Yes (at threshold) |
| `security:bouncer:monitor` | 5 | No (below threshold, event only) |
| `security:bouncer:event` | 5-7 | Depends on level |

The LLM agent and rule engine can triage these. The gap is admin visibility, time-series tracking, and default enforcement rules.

### 3. No default rules for confirmed bot enforcement

Bouncer events create incidents, but no default rules exist to auto-block confirmed bots at the firewall level. Today, bouncer "block" is application-level only — it prevents token issuance and serves decoys, but the IP can still hit every other endpoint.

High-confidence detections should escalate to firewall blocks:

| Category | Confidence | Should firewall block? |
|----------|-----------|----------------------|
| `security:bouncer:honeypot_post` | Very high — actively stuffing credentials | Yes, block 1 hour |
| `security:bouncer:campaign` | Very high — coordinated attack | Yes, block 24 hours |
| `security:bouncer:block` (score 80+) | High — strong bot signals | Yes, block 1 hour |
| `security:bouncer:block` (score 60-79) | Medium — might be misconfigured client | No — bouncer handled it |
| `security:bouncer:monitor` | Low | No |

The enforcement path is: bouncer event → incident → rule match → `block://` handler → firewall block fleet-wide. This keeps enforcement in the incident pipeline where it's auditable, configurable, and reversible.

## What to Change

### 1. Add bouncer metrics

Add `metrics.record()` calls at key decision points. Use category `"bouncer"` for batch fetching.

**In `assess.py` — scoring decisions:**

```python
from mojo.apps import metrics

# After scoring result (all decisions)
metrics.record("bouncer:assessments", category="bouncer")

# On block decision
metrics.record("bouncer:blocks", category="bouncer")
metrics.record(f"bouncer:blocks:country:{country_code}", category="bouncer")

# On monitor decision
metrics.record("bouncer:monitors", category="bouncer")
```

**In `views.py` — pre-screen and honeypot:**

```python
# Pre-screen signature cache hit (serves decoy without full scoring)
metrics.record("bouncer:pre_screen_blocks", category="bouncer")

# Honeypot POST (credential attempt on decoy page)
metrics.record("bouncer:honeypot_catches", category="bouncer")
```

**In `learner.py` — signature learning:**

```python
# New bot signature created/escalated
metrics.record("bouncer:signatures_learned", category="bouncer")

# Campaign detected
metrics.record("bouncer:campaigns", category="bouncer")
```

**Metric summary:**

| Slug | Where | What it tracks |
|------|-------|---------------|
| `bouncer:assessments` | assess.py | Total scoring runs (volume indicator) |
| `bouncer:blocks` | assess.py | Full-scoring blocks |
| `bouncer:blocks:country:{CC}` | assess.py | Blocks by country |
| `bouncer:monitors` | assess.py | Suspicious but allowed |
| `bouncer:pre_screen_blocks` | views.py | Signature cache hits (served decoy) |
| `bouncer:honeypot_catches` | views.py | Credential attempts on decoy pages |
| `bouncer:signatures_learned` | learner.py | Auto-created bot signatures |
| `bouncer:campaigns` | learner.py | Coordinated bot campaign detections |

### 2. Add SEARCH_FIELDS to bouncer models

The admin REST APIs exist but lack search support. Add SEARCH_FIELDS to RestMeta:

**BouncerDevice:**
```python
SEARCH_FIELDS = ['muid', 'duid', 'fingerprint_id', 'last_seen_ip']
```

**BouncerSignal:**
```python
SEARCH_FIELDS = ['muid', 'duid', 'ip_address', 'decision']
```

**BotSignature:**
```python
SEARCH_FIELDS = ['sig_type', 'value', 'source']
```

### 3. Add default bouncer enforcement rules

Extend `RuleSet.ensure_default_rules()` (or add a parallel `ensure_bouncer_rules()`) to create default rules for confirmed bot enforcement. Uses the same `_create_ruleset` helper with `get_or_create` (idempotent).

**Honeypot credential stuffing — always block:**
```python
cls._create_ruleset(
    category="security:bouncer:honeypot_post",
    name="Bouncer - Honeypot Credential Stuffing",
    priority=1,
    match_by=MatchBy.ALL,
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=30,
    handler="block://?ttl=3600&fleet_wide=1",
    rules=[
        {"name": "Level >= 9", "field_name": "level",
         "comparator": ">=", "value": "9", "value_type": "int"},
    ],
)
```

**Coordinated bot campaign — block longer:**
```python
cls._create_ruleset(
    category="security:bouncer:campaign",
    name="Bouncer - Bot Campaign Detection",
    priority=1,
    match_by=MatchBy.ALL,
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=60,
    handler="block://?ttl=86400&fleet_wide=1,notify://perm@manage_security",
    rules=[
        {"name": "Level >= 10", "field_name": "level",
         "comparator": ">=", "value": "10", "value_type": "int"},
    ],
)
```

**High-confidence bouncer block (score 80+) — block IP:**
```python
cls._create_ruleset(
    category="security:bouncer:block",
    name="Bouncer - High Confidence Bot Block",
    priority=1,
    match_by=MatchBy.ALL,
    bundle_by=BundleBy.SOURCE_IP,
    bundle_minutes=30,
    handler="block://?ttl=3600&fleet_wide=1",
    rules=[
        {"name": "Risk score >= 80", "field_name": "risk_score",
         "comparator": ">=", "value": "80", "value_type": "int"},
    ],
)
```

**Medium-confidence bouncer block (60-79) — no block, let LLM decide:**

No rule created. These events still create incidents (level 8 > threshold 7), and the LLM agent or a human can decide whether to escalate based on context (IP history, repeat offenses, etc.).

These rules are called from a new `ensure_bouncer_rules()` classmethod, invoked lazily on first bouncer assess (same pattern as OSSEC's `_ensure_defaults`).

### 4. Update bouncer.md with admin visibility section — DONE

Added to `docs/web_developer/account/bouncer.md`:

- Device reputation API (list, search, detail) with field reference and query examples
- Signal audit trail API (list, detail with full payloads) with graph reference
- Bot signature management API (list, create, update, delete) with type reference
- Bouncer events in incident system — flow diagram, category table, default rule actions
- Metrics section with all slugs and query examples
- Dashboard patterns — overview card, block feed, device investigation, chart ideas

### 5. Update security overview docs — DONE

Updated `docs/web_developer/security/README.md`:

- Added bouncer admin APIs to "APIs at a Glance" table
- Added "Bouncer Status" section to dashboard building guide
- Added bouncer metrics to metrics query examples and slug table
- Renumbered sections

## What NOT to Do

- **Don't create new models** — BouncerDevice, BouncerSignal, and BotSignature already capture everything needed
- **Don't add metrics for allow decisions** — high volume, low signal. Track assessments (total) and blocks/monitors (actionable)
- **Don't track token issuance metrics** — token_invalid events already flow through incidents, no need for separate metrics
- **Don't add logit entries** — bouncer already uses its own logger + incident events. Logit is for admin-initiated actions (firewall), not automated detection

## Acceptance Criteria

### Metrics
- [x] `metrics.record()` calls added for: assessments, blocks, blocks:country, monitors, pre_screen_blocks, honeypot_catches, signatures_learned, campaigns
- [x] All bouncer metrics use `category="bouncer"` for batch fetching
- [x] Country-level block metrics recorded (`bouncer:blocks:country:{CC}`)

### Search
- [x] SEARCH_FIELDS added to BouncerDevice, BouncerSignal, BotSignature

### Default Rules
- [x] `ensure_bouncer_rules()` classmethod added to RuleSet
- [x] Honeypot credential stuffing rule: `security:bouncer:honeypot_post` → block 1hr
- [x] Bot campaign rule: `security:bouncer:campaign` → block 24hr + notify
- [x] High-confidence block rule: `security:bouncer:block` (score >= 80) → block 1hr
- [x] Medium-confidence blocks (60-79) left for LLM/human triage (no rule)
- [x] Lazy invocation on first bouncer assess (same pattern as OSSEC `_ensure_defaults`)

### Docs
- [x] `bouncer.md` updated with admin visibility section covering device/signal/signature APIs
- [x] `bouncer.md` includes useful admin query examples
- [x] `bouncer.md` includes metrics query examples and dashboard patterns
- [x] `bouncer.md` documents how bouncer events flow into incidents and trigger firewall blocks
- [x] `security/README.md` updated with bouncer metrics table and link
- [x] Existing client integration docs in `bouncer.md` unchanged (no regressions)
