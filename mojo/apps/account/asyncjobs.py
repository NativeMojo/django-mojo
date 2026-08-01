import requests
from mojo.helpers import dates, logit


def prune_notifications(job):
    from mojo.apps.account.models.notification import Notification
    Notification.objects.filter(expires_at__lt=dates.utcnow()).delete()


def refresh_bouncer_sig_cache(job):
    """Scheduled job: rebuild Redis signature cache from active BotSignature records."""
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    refresh_sig_cache()


def inactive_sweep(job):
    """Nightly sweep: warn and disable inactive users and groups."""
    from mojo.helpers.settings import settings
    from mojo.helpers import logit

    results = {}

    if settings.get("ACCOUNT_AUTO_DISABLE_ENABLED", False):
        from mojo.apps.account.services.inactive import (
            _clear_stale_warnings, warn_inactive_users, disable_inactive_users,
        )
        from mojo.apps.account.models import User
        cleared = _clear_stale_warnings(User, settings.get("ACCOUNT_INACTIVE_DAYS", 90))
        warned = warn_inactive_users()
        disabled = disable_inactive_users()
        results["users"] = {"warnings_cleared": cleared, "warned": warned, "disabled": disabled}
        logit.info(f"Inactive user sweep: {cleared} warnings cleared, {warned} warned, {disabled} disabled")

    if settings.get("GROUP_AUTO_DISABLE_ENABLED", False):
        from mojo.apps.account.services.inactive import (
            _clear_stale_warnings, warn_inactive_groups, disable_inactive_groups,
        )
        from mojo.apps.account.models import Group
        cleared = _clear_stale_warnings(Group, settings.get("GROUP_INACTIVE_DAYS", 90))
        warned = warn_inactive_groups()
        disabled = disable_inactive_groups()
        results["groups"] = {"warnings_cleared": cleared, "warned": warned, "disabled": disabled}
        logit.info(f"Inactive group sweep: {cleared} warnings cleared, {warned} warned, {disabled} disabled")

    return results


def push_abuse_signals(job):
    """
    Push observed abuse-signal updates to an upstream mojo GeoIP provider.

    Payload: {ip, threat_level?, is_known_attacker?, is_known_abuser?}
    Any subset of the three signal fields plus the required ip.

    Behavior:
      - 2xx: success.
      - 4xx (auth / perm / validation): log and return without retry.
      - 5xx or network error: raise so the engine retries with backoff.
      - Missing config (URL or API key): log and return without retry.

    Called via jobs.publish() from GeoLocatedIP._maybe_push_abuse_signals().
    """
    from mojo.helpers.geoip import config as geoip_config
    # Lazy import, as the incident plane depends on models that are not needed
    # for the common success path. report_event_suppressed never raises.
    from mojo.apps.incident import report_event_suppressed

    payload = job.payload or {}
    ip = payload.get("ip")
    if not ip:
        # Key NAMES only. The payload can carry abuse state (threat_level, the
        # is_known_* flags); echoing values into an incident would leak it.
        report_event_suppressed(
            f"push_abuse_signals dropped a job with no 'ip'. Present payload "
            f"keys were {sorted(payload.keys())[:10]}. An abuse-signal push "
            f"cannot address an upstream record without the IP.",
            key="geoip:abuse_push_alerted:missing_ip",
            title="Abuse-signal push: payload missing ip",
            category="geoip:abuse_push_missing_ip",
            level=4, request=None)
        return

    # Only forward the federated abuse-signal fields. Any other key in the
    # payload is silently discarded — defense in depth against accidental
    # firewall-state leakage.
    body = {"ip": ip}
    for field in ("threat_level", "is_known_attacker", "is_known_abuser"):
        if field in payload:
            body[field] = payload[field]

    if len(body) == 1:
        # Key NAMES only — the values are the abuse state itself.
        report_event_suppressed(
            f"push_abuse_signals dropped a job carrying an ip but no signal "
            f"fields. Present payload keys were {sorted(payload.keys())[:10]}. "
            f"With nothing to sync there is no point contacting the upstream "
            f"provider.",
            key="geoip:abuse_push_alerted:no_signals",
            title="Abuse-signal push: no signal fields",
            category="geoip:abuse_push_no_signals",
            level=4, request=None)
        return

    base_url = geoip_config.MOJO_PROVIDER_URL
    api_key = geoip_config.get_api_key("mojo")
    if not base_url or not api_key:
        # Name the SETTINGS, never their values — the URL and api key are secret.
        report_event_suppressed(
            "push_abuse_signals cannot sync: GEOIP_MOJO_PROVIDER_URL or "
            "GEOIP_API_KEY_MOJO is unset. Outbound abuse-signal push-back is "
            "enabled but has no upstream to reach, so every push is being "
            "dropped without retry.",
            key="geoip:abuse_push_alerted:unconfigured",
            title="Abuse-signal push: upstream not configured",
            category="geoip:abuse_push_unconfigured",
            level=5, request=None)
        return

    url = f"{base_url.rstrip('/')}/api/system/geoip/sync"
    headers = {
        "Authorization": f"apikey {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=10)
    except requests.RequestException as e:
        # Network-level failure — raise so the engine retries with backoff.
        raise RuntimeError(f"push_abuse_signals network error for {ip}: {e}")

    if 200 <= response.status_code < 300:
        # f-string, not printf: logit's _build_log newline-joins its args, so a
        # "%s -> %s" template never interpolates (arg-0 is a cosmetic channel).
        logit.info(f"push_abuse_signals: synced {ip} -> {body}")
        return

    if 400 <= response.status_code < 500:
        # Auth, perm, validation — won't be fixed by retry. Keyed per status so a
        # 401 (bad key) and a 422 (bad body) stay distinct in the feed. f-string,
        # not printf — the old "%s/%d/%r" template never interpolated.
        report_event_suppressed(
            f"push_abuse_signals: upstream rejected ip {str(ip)[:45]} with HTTP "
            f"{response.status_code}, not retrying. Response body: "
            f"{response.text[:200]!r}",
            key=f"geoip:abuse_push_alerted:rejected:{response.status_code}",
            title=f"Abuse-signal push rejected (HTTP {response.status_code})",
            category="geoip:abuse_push_rejected",
            level=4, request=None,
            status_code=response.status_code)
        return

    # 5xx (or anything else) — raise to retry.
    raise RuntimeError(
        f"push_abuse_signals: upstream returned {response.status_code} for {ip}"
    )
