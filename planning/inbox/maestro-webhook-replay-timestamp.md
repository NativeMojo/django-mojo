---
# id is assigned by /scope on pickup — leave it blank
id:
type: chore
title: Maestro webhook replay protection — timestamp/nonce in the signed payload
priority: P3
effort: S
owner: backend
opened: 2026-07-16
depends_on: []         # soft: maestro repo must add the timestamp to its signed payload first
related: [DM-040]
links: []
---

# Maestro webhook replay protection — timestamp/nonce in the signed payload

## What & Why

DM-040's security review flagged that maestro board webhooks
(`POST /api/incident/maestro/webhook/<callback_token>`, signed
`X-Mojo-Signature` HMAC over the canonical payload dict) carry no
timestamp or nonce, so a captured valid (token, payload, signature) triple
can be replayed indefinitely. DM-040 shipped partial mitigation client-side:
`note.created` dedups on the board-side note id, and status application is
compare-before-write — but a replayed `item.updated` can still re-apply a
stale status (e.g. re-close a ticket a human reopened).

Full fix is a **cross-repo contract change**: maestro must include a
timestamp (`"ts"`) — and ideally a unique event id — inside the signed
payload, and this side must reject deliveries outside a small window and/or
dedup on event id. The maestro repo's linking contract
(`maestro/api` — `planning/confirmed/maestro-connect.md`, future
`docs/web_developer/boards/linking.md`) needs the matching change; sequence
this item after maestro ships it (or file the twin item there first).

## Acceptance Criteria
- [ ] Maestro-side contract includes `ts` (epoch seconds) inside every signed
      webhook payload (twin item in the maestro repo; blocked until then)
- [ ] `mojo/apps/incident/rest/maestro_webhook.py` (or the service) rejects
      payloads whose `ts` is outside a configurable window (e.g.
      `MAESTRO_WEBHOOK_MAX_AGE`, default ~300s) — fail-closed 401, tolerant
      of a missing `ts` only behind a compat setting during rollout
- [ ] Tests: fresh payload accepted, stale payload rejected, missing-ts
      behavior per compat setting
- [ ] Docs updated in both tracks (`security/maestro_board.md`)

## Repro — bugs only
1.
- Expected:
- Actual:

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

_Write a complete, self-contained design here — enough that a fresh session can
`/build` it cold, without re-deriving anything. Fill every subsection._

### Goal
[One sentence.]

### Context — what exists
[The recon a builder would otherwise redo: relevant files with paths and
`file:line` refs, current behavior, key snippets, helpers/patterns to reuse.]

### Changes — what to do
1. `path` — [exact change and why]
2. `path` — [...]

### Design decisions
- [decision] — [rationale; alternatives rejected]

### Edge cases & risks
- [case] — [how it's handled]

### Tests
- [scenario] -> `test file`   (for a bug: the regression test to add)

### Docs
- `doc` — [what changes]

### Open questions
- Window size and clock-skew tolerance; whether to also dedup on a maestro
  event id (needs maestro to mint one)

## Notes

Origin: DM-040 post-build security review (2026-07-16), WARNING "Replay".
Client-side partial mitigations already shipped in DM-040 (note dedup,
compare-before-write status).

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
