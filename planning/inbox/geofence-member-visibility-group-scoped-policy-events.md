---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature
title: Member-readable geofence policy + events — group-scoped visibility for brand admins (not just global platform staff)
priority: P2
effort: M
owner:
opened: 2026-07-08
depends_on: []
related: [ITEM-017, mverify_portal#ITEM-014]
links: []
---

# Member-readable geofence policy + events — group-scoped visibility for brand admins

## What & Why

ITEM-017's REST contract (`GET /api/geo/rules`, incident Events) and the
consuming portal page (mverify_portal ITEM-014) are both, by design,
**platform-admin only**: every read requires a GLOBAL user-level permission
(`view_geofence`/`manage_geofence`/`security` for rules; `view_security`/
`security` for events) — member/group-scoped grants explicitly do not
authorize (`mojo/apps/account/rest/geofence.py` docstring: "these endpoints
manage platform-wide config"). That's correct for the global config surface,
but it means a brand's OWN admin (a GroupMember of a single tenant/platform,
not a platform-wide operator) cannot see geofencing status for their own
platform at all — even though it is actively enforcing on their sign-up and
checkout pages right now.

This item is the group-scoped counterpart: let a member with an appropriate
per-group permission see (a) the effective geofencing policy for **their own
group only**, and (b) enforcement events for their own group — without
exposing platform-wide internals (other groups' rules, full posture
operational detail, exemption counts, `enforced_endpoints`) to them.

Raised during mverify_portal ITEM-014's design review (2026-07-08: "is this
not admin, what about by platform/group?") and left as an open question
until now.

## Acceptance Criteria

- [ ] A group-scoped, read-only path exists for a caller holding an
      appropriate **per-group** permission to fetch the effective
      geofencing policy for **their own group only** — NOT a loosened
      version of the existing global endpoint's full response. Investigate
      first: can `/api/geo/rules` support a member-scoped mode with a
      deliberately narrower payload (e.g. plain "baseline + your group's
      rules", omitting `enforced_endpoints`/`cache_ttl`/`allowlist_summary`/
      other-group data), or does this want a distinct endpoint? Decide and
      justify in the plan.
- [ ] **Verify before designing**: does the framework's existing
      group-scoped list fallback (`mojo/models/rest.py`'s `on_rest_handle_list`
      — authorizes GROUP-scoped `view_security`/`security` grants filtered to
      `group__in=<the caller's groups>`) already work for Incident Event
      access to `geofence_block`/`geofence_exempt`/`geofence_config`
      categories? If yes, this item's event-side work may just be
      confirming/exercising it (+ a regression test) rather than building
      new mechanics; if no, close the gap.
- [ ] No cross-tenant leakage: a group-scoped caller never sees another
      group's rules, another group's events, or global operational detail
      not relevant to their own platform (cache TTL, allowlist internals,
      enforced-endpoints list).
- [ ] Permission key(s) used are checked against product conventions before
      inventing new ones — e.g. does the calling product already have a
      group-scoped "compliance"-flavored role that should carry this, rather
      than a bespoke key? (mverify_portal's existing group-scoped pages use
      `admin_compliance`/`admin_verify`-style keys — check fit.)
- [ ] Docs (both tracks) distinguish the two audiences explicitly: platform
      staff (global, full config) vs. brand admin (group-scoped, their
      platform's effective policy + events only).
- [ ] Tests: a group-scoped grant sees only their own group's data; a
      global-only grant continues to work unchanged; a group-scoped grant
      without the new permission still gets 403.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- **Not urgent / not blocking**: the platform-admin surface (ITEM-017 +
  mverify_portal ITEM-014) already fully serves the compliance-evidence use
  case (Coinflow, etc.). This is a distinct audience/capability, not a fix.
- **Portal-side consequence, once this lands**: mverify_portal's
  `GeofencingPage.js` already degrades per-card by permission (built that
  way deliberately) — a group-scoped grant holder visiting the SAME page
  today would see "Not permitted" on every card. Once a group-scoped read
  path exists, either (a) the existing page's permission checks widen to
  accept the new group-scoped grant and the page naturally lights up scoped
  to their own group, or (b) a distinct member-facing page is filed
  separately in mverify_portal — decide once the backend shape is chosen.
- **Design trap to avoid**: do not just relax `requires_global_perms` on the
  existing `/api/geo/rules` endpoint — its current response includes
  platform-wide operational detail (posture internals, allowlist counts,
  `enforced_endpoints` for every scope) that a single brand's staff should
  not see. The response shape for a member-scoped caller needs to be
  deliberately narrower, not just differently authorized.
- Sibling filed same day: `geofence-group-scoped-metrics.md` (dual-write
  `account=group-<id>` for block/exempt metrics) — related but distinct:
  that item is about WHERE metrics are recorded; this item is about WHO can
  read policy/events for their own group. Both are needed for a fully
  group-scoped experience.
