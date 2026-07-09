"""Evidence plane for geofence enforcement.

Every enforcement outcome that matters to a compliance auditor becomes an
incident Event (mojo.apps.incident):

  geofence_block  — a request was denied, or was allowed only because the
                    deployment fails open on a geo-lookup failure
  geofence_exempt — an allowlisted IP passed where the rules would have blocked
  geofence_config — a rules / allowlist / per-IP whitelist change (this event
                    stream IS the config-plane change history)

Event levels (INCIDENT_LEVEL_THRESHOLD default 7 auto-creates an Incident):
  7  rule_invalid at evaluation      — a broken rule is denying traffic; pages
  6  lookup failure while fail-open  — enforcement silently not happening
  5  abuse-flag block, or a block on a fail-closed scope (money endpoints)
  3  ordinary jurisdiction block / exemption-used / config change

Block and exempt events are deduped per (ip, reason) per hour so a blocked
client hammering an endpoint cannot flood the event stream; aggregate metrics
count every block including deduped ones. Nothing in this module may raise
into the request path — every public function swallows and logs its failures.
"""
from mojo.helpers import logit
from mojo.helpers.redis import get_connection
from mojo.apps import metrics


DEDUPE_TTL = 3600  # one event per (ip, reason) per hour

_ABUSE_REASONS = {"tor_detected", "vpn_detected", "proxy_detected", "datacenter_detected"}


def report_block(request, decision, scope=None):
    """Record a geofence block — or a fail-open lookup-failure allow."""
    try:
        _report_block(request, decision, scope)
    except Exception as exc:
        logit.error("geofence", f"evidence report_block failed: {exc}")


def _report_block(request, decision, scope):
    blocked = decision.allowed is False
    if blocked:
        _record_block_metrics(decision, getattr(request, "group", None))
    if not _dedupe_wins(decision.ip, decision.reason):
        return
    from mojo.apps.incident import reporter
    verb = "blocked" if blocked else "fail-open allowed"
    reporter.report_event(
        f"Geofence {verb} {decision.ip} ({decision.reason}) on {request.path}",
        title=f"Geofence {'block' if blocked else 'fail-open'}: {decision.reason}",
        category="geofence_block",
        level=_block_level(request, decision, scope),
        request=request,
        reason=decision.reason,
        rule_level=decision.get("rule_level"),
        geofence_scope=scope,
        country_code=decision.get("country_code"),
        region_code=decision.get("region_code"),
        abuse=dict(decision.get("abuse") or {}),
        detail=decision.get("detail"),
    )


def report_exempt(request, decision, scope=None):
    """Record an allowlisted pass that would otherwise have blocked."""
    try:
        # every occurrence counts, deduped or not
        metrics.record("geofence:exempt", category="geofence")
        group = getattr(request, "group", None)
        if group is not None:
            metrics.record("geofence:exempt", category="geofence",
                           account=f"group-{group.pk}")
        if not _dedupe_wins(decision.ip, decision.reason):
            return
        from mojo.apps.incident import reporter
        reporter.report_event(
            f"Geofence exemption used by {decision.ip} on {request.path} "
            f"(would have blocked: {decision.get('would_block_reason')})",
            title="Geofence exemption used",
            category="geofence_exempt",
            level=3,
            request=request,
            reason=decision.reason,
            geofence_scope=scope,
            allowlist_source=decision.get("allowlist_source"),
            allowlist_reason=decision.get("allowlist_reason"),
            would_block_reason=decision.get("would_block_reason"),
            country_code=decision.get("country_code"),
            region_code=decision.get("region_code"),
        )
    except Exception as exc:
        logit.error("geofence", f"evidence report_exempt failed: {exc}")


def report_config_change(target, old, new, request=None, user=None):
    """Record a config-plane change (rules / allowlist / per-IP whitelist).

    Never deduped — every change is history; the admin UI queries this stream
    (category="geofence_config") for who/when/what. `request` is optional so
    model-layer hooks can call it; attribution falls back to `user`.
    """
    try:
        if user is None and request is not None:
            candidate = getattr(request, "user", None)
            if candidate is not None and getattr(candidate, "is_authenticated", False):
                user = candidate
        username = getattr(user, "username", None)
        from mojo.apps.incident import reporter
        reporter.report_event(
            f"Geofence config changed: {target}"
            + (f" by {username}" if username else ""),
            title=f"Geofence config change: {target}",
            category="geofence_config",
            level=3,
            request=request,
            target=target,
            old=old,
            new=new,
            changed_by=username,
            changed_by_id=getattr(user, "id", None),
        )
    except Exception as exc:
        logit.error("geofence", f"evidence report_config_change failed: {exc}")


def _block_level(request, decision, scope):
    if decision.reason == "rule_invalid":
        return 7
    if decision.reason == "lookup_failed" and decision.allowed:
        return 6
    if decision.reason in _ABUSE_REASONS or _scope_fails_closed(request, scope):
        return 5
    return 3


def _scope_fails_closed(request, scope):
    if not scope:
        return False
    from .engine import _list_setting_with_header
    scopes = _list_setting_with_header(
        request, "X-Mojo-Test-Geofence-Fail-Closed-Scopes",
        "GEOFENCE_FAIL_CLOSED_SCOPES", [])
    return scope in scopes


def _dedupe_wins(ip, reason):
    """True when this (ip, reason) hasn't fired within the window.
    On Redis failure, emit anyway — evidence beats dedupe."""
    try:
        return bool(get_connection().set(
            f"geofence:evt:{ip}:{reason}", 1, ex=DEDUPE_TTL, nx=True))
    except Exception as exc:
        logit.error("geofence", f"evidence dedupe failed: {exc}")
        return True


def _record_block_metrics(decision, group=None):
    """Aggregate counters — recorded for EVERY block, including deduped ones.
    Mirrors the firewall:blocks pattern (geolocated_ip.block)."""
    try:
        metrics.record("geofence:blocks", category="geofence")
        cc = decision.get("country_code")
        if cc:
            metrics.record(f"geofence:blocks:country:{cc}", category="geofence")
        rc = decision.get("region_code")  # ISO 3166-2, already country-prefixed (US-WA)
        if rc:
            metrics.record(f"geofence:blocks:region:{rc}", category="geofence")
        if group is not None:
            # base slug only — per-group country/region would cross-product keys
            metrics.record("geofence:blocks", category="geofence",
                           account=f"group-{group.pk}")
    except Exception as exc:
        logit.error("geofence", f"block metrics failed: {exc}")
