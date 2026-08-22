"""
Threat Intelligence module for checking IPs against various blocklists and
internal incident data.
"""
import requests
from objict import objict
from mojo.helpers.settings import settings
from mojo.helpers import dates, logit

# ---------------------------------------------------------------------------
# Detection defaults.
#
# Every name in this block is the DEFAULT for the matching GEOLOCATION_*
# setting, NOT the live value. _config() re-reads each one through
# settings.get() on every call, so a deployment can retune detection from the
# DB-backed Setting store with no code change and no restart — "each system
# tunes its own rules". get_static() (what this module used to call) reads
# file-based settings only and freezes the value at import time, which is why
# nothing here was tunable before.
#
# A file-based setting of the same name still wins over the default below; the
# DB-backed row wins over both.
# ---------------------------------------------------------------------------
ENABLE_BLOCKLIST_CHECK = settings.get_static('GEOLOCATION_ENABLE_BLOCKLIST_CHECK', True)
ENABLE_INTERNAL_THREAT_CHECK = settings.get_static('GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK', True)

# Display-only stat window: total_events, avg_level, top_categories and
# last_seen_event are reported over this many days because the admin UI wants
# the long view. It drives NO boolean — see INTERNAL_THREAT_WINDOW_HOURS.
INTERNAL_THREAT_LOOKBACK_DAYS = settings.get_static('GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS', 90)

# Predicate window. is_known_attacker / is_known_abuser answer "is this address
# hostile RIGHT NOW", and the enforcement they feed is measured in minutes. The
# old 90-day count answered "has anything ever gone wrong here", which on a
# shared egress is always yes. 24h still spans a slow stuffing run pacing under
# the rate limits, and lets an address rehabilitate in a day.
INTERNAL_THREAT_WINDOW_HOURS = settings.get_static('GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS', 24)

# Deprecated. Retained so an existing import does not break; no longer read by
# any predicate (the attacker tiers and the abuser check carry their own
# thresholds).
INTERNAL_THREAT_EVENT_THRESHOLD = settings.get_static('GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD', 5)

# Secondary severity floor, CONFIRMED TIER ONLY. The confirmed categories are
# already unambiguous by name, so this only exists to let an operator tighten.
# Default is 7, not 8, because `sensitive_field_probe` is emitted at exactly 7
# (mojo/models/rest.py) — a floor of 8 would silently make a third of the
# shipped confirmed list inert.
INTERNAL_ATTACKER_LEVEL_THRESHOLD = settings.get_static('GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD', 7)

# --- Tier 1: CONFIRMED -----------------------------------------------------
# Unambiguous. No legitimate user produces these: a honeypot field is invisible
# to a human, a campaign signature is a cross-device correlation, and a
# sensitive-field probe is a deliberate attempt to filter on a column the model
# declares off-limits. A handful is enough.
INTERNAL_ATTACKER_CONFIRMED_CATEGORIES = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES',
    ['security:bouncer:honeypot_post',
     'security:bouncer:campaign',
     'sensitive_field_probe'])
INTERNAL_ATTACKER_CONFIRMED_THRESHOLD = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD', 3)

# --- Tier 2: SUSPECT -------------------------------------------------------
# Ambiguous. A real user CAN produce one of these by fumbling a credential, so
# volume alone proves nothing — the tier also requires BREADTH (attempts spread
# across many distinct accounts) and is suppressed behind a shared egress.
#
# `invalid_password` is here despite being reported at level 5: it is the
# strongest credential-stuffing signal in the codebase and was invisible to the
# old level-8 floor. For this tier the category allowlist REPLACES the level
# floor — an attack category's severity is a reporting decision, not evidence.
INTERNAL_ATTACKER_SUSPECT_CATEGORIES = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES',
    ['login:unknown', 'reset:unknown', 'magic:unknown',
     'totp:login_unknown', 'sms:login_unknown', 'token:unknown',
     'invalid_password'])
INTERNAL_ATTACKER_SUSPECT_THRESHOLD = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD', 25)
# Breadth gate: how many DISTINCT accounts the suspect events must target. One
# user fumbling their own password hits exactly one. Spraying hits many.
INTERNAL_ATTACKER_MIN_TARGETS = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS', 10)

# --- Shared-egress suppressor ---------------------------------------------
# BouncerSignal.muid is a server-set HttpOnly cookie present pre-auth, so JS
# cannot forge it. An address fronting this many independent browsers inside
# the window is a NAT/CGNAT/corporate egress, not one attacker — suppress the
# SUSPECT tier there. Never the confirmed tier.
INTERNAL_SHARED_EGRESS_MIN_DEVICES = settings.get_static(
    'GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES', 25)

# --- Abuser ----------------------------------------------------------------
# Every rate_limit:* event is deduped to one per key+IP per 60s in Redis
# (mojo/decorators/limits.py), so each event is roughly one minute spent over a
# limit. 60 of them is an hour of the day sustained over the limits — a real
# abuse pattern, unlike the old "has this IP ever used the API" count.
INTERNAL_ABUSER_CATEGORY_PREFIX = settings.get_static(
    'GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX', 'rate_limit:')
INTERNAL_ABUSER_EVENT_THRESHOLD = settings.get_static(
    'GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD', 60)

# --- Dry run ---------------------------------------------------------------
# When true, compute the verdict and log it (with the counts that drove it) but
# always return False/False. Lets a busy deployment watch a week of real
# traffic before anything can act on a retune.
INTERNAL_THREAT_DRY_RUN = settings.get_static('GEOLOCATION_INTERNAL_THREAT_DRY_RUN', False)

# DEPRECATED denylist. Default is None (= unset) so "a deployment configured
# this" is distinguishable from "we shipped it". Still honored when set: the
# named categories are subtracted from both allowlists. The denylist shape was
# structurally wrong — every NEW level-8+ category silently became attack
# evidence, which is how `magic:unknown` and then `rest_error` (level 12, fired
# by ANY unhandled 500 and attributed to the client's IP) became "attacks".
INTERNAL_ATTACKER_EXCLUDED_CATEGORIES = settings.get_static(
    'GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES', None)

# model_name values that mean "this event targeted a user account". Instance
# reporting (User.report_incident) stamps the app-qualified form; class-level
# reporting (User.class_report_incident, used by login:unknown) stamps the bare
# class name and no model_id at all — an event with no model_id is not a
# target, which is why a username-enumeration burst can never satisfy the
# breadth gate on its own.
ATTACK_TARGET_MODEL_NAMES = ('account.User', 'User')

_DEPRECATION_WARNED = False


def _config():
    """Read every detection threshold at call time.

    DB-backed Setting rows win, then file-based settings, then the module
    default bound above. Reading the module attribute as the default is what
    keeps `mock.patch.object(threat_intel, "X", ...)` working: _config()
    resolves the global at call time, and an unset setting falls through to it.
    """
    global _DEPRECATION_WARNED

    excluded = settings.get(
        'GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES',
        INTERNAL_ATTACKER_EXCLUDED_CATEGORIES, kind="list")
    if excluded and not _DEPRECATION_WARNED:
        _DEPRECATION_WARNED = True
        logit.warning(
            "geoip",
            "GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES is deprecated: "
            "attacker detection is now a two-tier allowlist "
            "(GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES / "
            "..._SUSPECT_CATEGORIES) and everything unnamed already counts for "
            f"nothing. Still honoring the exclusion of {list(excluded)!r}; "
            "remove the setting and edit the allowlists instead.")

    return objict(
        enabled=settings.get('GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK',
                             ENABLE_INTERNAL_THREAT_CHECK, kind="bool"),
        lookback_days=settings.get('GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS',
                                   INTERNAL_THREAT_LOOKBACK_DAYS, kind="int"),
        window_hours=settings.get('GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS',
                                  INTERNAL_THREAT_WINDOW_HOURS, kind="int"),
        attacker_level_threshold=settings.get(
            'GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD',
            INTERNAL_ATTACKER_LEVEL_THRESHOLD, kind="int"),
        confirmed_categories=settings.get(
            'GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES',
            INTERNAL_ATTACKER_CONFIRMED_CATEGORIES, kind="list"),
        confirmed_threshold=settings.get(
            'GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD',
            INTERNAL_ATTACKER_CONFIRMED_THRESHOLD, kind="int"),
        suspect_categories=settings.get(
            'GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES',
            INTERNAL_ATTACKER_SUSPECT_CATEGORIES, kind="list"),
        suspect_threshold=settings.get(
            'GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD',
            INTERNAL_ATTACKER_SUSPECT_THRESHOLD, kind="int"),
        min_targets=settings.get('GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS',
                                 INTERNAL_ATTACKER_MIN_TARGETS, kind="int"),
        shared_egress_min_devices=settings.get(
            'GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES',
            INTERNAL_SHARED_EGRESS_MIN_DEVICES, kind="int"),
        abuser_prefix=settings.get('GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX',
                                   INTERNAL_ABUSER_CATEGORY_PREFIX),
        abuser_threshold=settings.get('GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD',
                                      INTERNAL_ABUSER_EVENT_THRESHOLD, kind="int"),
        dry_run=settings.get('GEOLOCATION_INTERNAL_THREAT_DRY_RUN',
                             INTERNAL_THREAT_DRY_RUN, kind="bool"),
        excluded_categories=list(excluded or []),
    )


def _allowlists(cfg):
    """The two category tiers with any deprecated exclusions removed."""
    excluded = set(cfg.excluded_categories or [])
    confirmed = [c for c in (cfg.confirmed_categories or []) if c not in excluded]
    suspect = [c for c in (cfg.suspect_categories or []) if c not in excluded]
    return confirmed, suspect


def _distinct_devices(ip_address, window_start):
    """DISTINCT muids seen behind this IP inside the window, or None.

    None means "no answer" — the deployment does not run the bouncer JS, or the
    account app is unavailable. Callers must treat None as "cannot tell" and
    never as zero.

    Only muids whose BouncerDevice.first_seen predates the window are counted,
    so an attacker cycling fresh muids on one address cannot manufacture a fake
    NAT and suppress their own detection.
    """
    try:
        from mojo.apps.account.models.bouncer_signal import BouncerSignal
        from mojo.apps.account.models.bouncer_device import BouncerDevice

        seen = (BouncerSignal.objects
                .filter(ip_address=ip_address, created__gte=window_start)
                .exclude(muid='')
                .order_by()
                .values_list('muid', flat=True))
        if not seen.exists():
            # No bouncer telemetry at all for this address — skip the
            # suppressor entirely rather than defaulting either way.
            return None
        # muid is unique on BouncerDevice, so counting devices IS counting
        # distinct qualifying muids.
        return BouncerDevice.objects.filter(
            muid__in=seen, first_seen__lt=window_start).count()
    except Exception as e:
        logit.exception("geoip",
                        f"shared-egress device count failed for {ip_address}: {e}")
        return None


def _window_stats(ip_address, cfg):
    """Attack-activity counters for one IP inside the predicate window.

    Pure counting — no verdict. check_internal_threats() applies the
    predicates; Event exposes the same numbers as rule-matchable properties.
    """
    from datetime import timedelta
    from mojo.apps.incident.models.event import Event

    window_start = dates.utcnow() - timedelta(hours=cfg.window_hours)
    confirmed_cats, suspect_cats = _allowlists(cfg)

    recent = Event.objects.filter(source_ip=ip_address, created__gte=window_start)

    confirmed_events = 0
    if confirmed_cats:
        confirmed_events = recent.filter(
            category__in=confirmed_cats,
            level__gte=cfg.attacker_level_threshold).count()

    suspect_events = 0
    distinct_targets = 0
    if suspect_cats:
        suspect_qs = recent.filter(category__in=suspect_cats)
        suspect_events = suspect_qs.count()
        if suspect_events:
            distinct_targets = (suspect_qs
                                .filter(model_name__in=ATTACK_TARGET_MODEL_NAMES,
                                        model_id__isnull=False)
                                .order_by()
                                .values('model_id')
                                .distinct()
                                .count())

    abuse_events = 0
    if cfg.abuser_prefix:
        abuse_events = recent.filter(category__startswith=cfg.abuser_prefix).count()

    return objict(
        window_start=window_start,
        window_hours=cfg.window_hours,
        confirmed_events=confirmed_events,
        suspect_events=suspect_events,
        attack_events=confirmed_events + suspect_events,
        distinct_targets=distinct_targets,
        abuse_events=abuse_events,
    )


def ip_activity_stats(ip_address):
    """Window-scoped attack counters for an IP, safe to call from any path.

    Backs the Event.ip_recent_* properties so an operator can write incident
    rules against a rate instead of a single event. Never raises: a failure
    yields zeros and a None device count (both of which fail a rule closed).
    """
    blank = objict(confirmed_events=0, suspect_events=0, attack_events=0,
                   distinct_targets=0, abuse_events=0, distinct_devices=None,
                   window_hours=INTERNAL_THREAT_WINDOW_HOURS)
    if not ip_address:
        return blank
    try:
        cfg = _config()
        stats = _window_stats(ip_address, cfg)
        stats.distinct_devices = _distinct_devices(ip_address, stats.window_start)
        return stats
    except Exception as e:
        logit.exception("geoip", f"ip_activity_stats failed for {ip_address}: {e}")
        return blank


def _get_blocklist_config():
    """Build blocklist config at call time so API keys come from DB."""
    return {
        'abuseipdb': {
            'enabled': settings.get('THREAT_INTEL_ABUSEIPDB_ENABLED', False),
            'api_key': settings.get('THREAT_INTEL_ABUSEIPDB_API_KEY', None),
            'url': 'https://api.abuseipdb.com/api/v2/check',
        },
        'blocklist_de': {
            'enabled': settings.get_static('THREAT_INTEL_BLOCKLIST_DE_ENABLED', True),
            'url': 'https://lists.blocklist.de/lists/all.txt',
        },
        'spamhaus': {
            'enabled': settings.get_static('THREAT_INTEL_SPAMHAUS_ENABLED', False),
        }
    }


def check_internal_threats(ip_address):
    """
    Check internal incident database for threats from this IP.
    Returns dict with is_known_attacker, is_known_abuser, and stats.

    is_known_attacker is a two-tier ALLOWLIST verdict over a short window:

      * CONFIRMED — a few events in a category no legitimate user can produce.
      * SUSPECT   — many events in a category a real user CAN produce, spread
                    across many distinct accounts, from an address that is not
                    a shared egress.

    Anything not named in either tier counts toward NOTHING. That is deliberate
    and is the point of the rewrite: an allowlist cannot be silently widened by
    a new category, and — critically — the bouncer's own decisions
    (security:bouncer:block, :session_*) are not evidence, which breaks the
    self-confirming loop where a block raised the score that produced the next
    block.
    """
    cfg = _config()
    if not cfg.enabled:
        return {
            'is_known_attacker': False,
            'is_known_abuser': False,
            'internal_stats': {}
        }

    try:
        from mojo.apps.incident.models.event import Event
        from datetime import timedelta
        # Lazy on purpose (see detection._cached_ip_set) — but it MUST be bound
        # before its first use below, or every call past the early return dies
        # with UnboundLocalError inside the blanket except.
        from django.db import models

        lookback_date = dates.utcnow() - timedelta(days=cfg.lookback_days)

        # Display-only history. These stats feed the admin UI over the long
        # window; none of them drives a boolean any more.
        events = Event.objects.filter(
            source_ip=ip_address,
            created__gte=lookback_date
        )

        total_events = events.count()

        if total_events == 0:
            return {
                'is_known_attacker': False,
                'is_known_abuser': False,
                'internal_stats': {'total_events': 0}
            }

        avg_level = events.aggregate(avg_level=models.Avg('level'))['avg_level'] or 0

        # Get category breakdown
        category_counts = events.values('category').annotate(
            count=models.Count('id')
        ).order_by('-count')[:5]

        # Get most recent event
        recent_event = events.order_by('-created').first()

        # --- predicates: short window, allowlisted categories only ---------
        window = _window_stats(ip_address, cfg)

        is_confirmed = window.confirmed_events >= cfg.confirmed_threshold
        is_suspect = (window.suspect_events >= cfg.suspect_threshold and
                      window.distinct_targets >= cfg.min_targets)

        # Shared-egress suppressor. Suspect tier ONLY — a confirmed attacker
        # behind a corporate NAT is still an attacker.
        distinct_devices = None
        suppressed = False
        if is_suspect and not is_confirmed:
            distinct_devices = _distinct_devices(ip_address, window.window_start)
            if (distinct_devices is not None and
                    distinct_devices >= cfg.shared_egress_min_devices):
                is_suspect = False
                suppressed = True

        is_known_attacker = bool(is_confirmed or is_suspect)
        is_known_abuser = window.abuse_events >= cfg.abuser_threshold

        stats = {
            'total_events': total_events,
            # Attack evidence inside the predicate window. Kept under the old
            # key because recalculate_threat_level() scores it.
            'high_severity_events': window.attack_events,
            'confirmed_events': window.confirmed_events,
            'suspect_events': window.suspect_events,
            'distinct_targets': window.distinct_targets,
            'distinct_devices': distinct_devices,
            'shared_egress_suppressed': suppressed,
            'abuse_events': window.abuse_events,
            'avg_level': round(avg_level, 2),
            'top_categories': list(category_counts),
            'last_seen_event': recent_event.created.isoformat() if recent_event else None,
            'lookback_days': cfg.lookback_days,
            'window_hours': cfg.window_hours,
        }

        if cfg.dry_run:
            # Compute everything, act on nothing. Lets a busy deployment watch
            # a week of real traffic before a retune can block anyone.
            stats['dry_run'] = True
            stats['dry_run_is_known_attacker'] = is_known_attacker
            stats['dry_run_is_known_abuser'] = is_known_abuser
            logit.info(
                "geoip",
                f"[dry-run] internal threat verdict for {ip_address}: "
                f"attacker={is_known_attacker} abuser={is_known_abuser} "
                f"confirmed={window.confirmed_events}/{cfg.confirmed_threshold} "
                f"suspect={window.suspect_events}/{cfg.suspect_threshold} "
                f"targets={window.distinct_targets}/{cfg.min_targets} "
                f"devices={distinct_devices} suppressed={suppressed} "
                f"abuse={window.abuse_events}/{cfg.abuser_threshold} "
                f"window={cfg.window_hours}h — returning False/False")
            return {
                'is_known_attacker': False,
                'is_known_abuser': False,
                'internal_stats': stats
            }

        return {
            'is_known_attacker': is_known_attacker,
            'is_known_abuser': is_known_abuser,
            'internal_stats': stats
        }

    except Exception as e:
        # exception() (not error()) so the traceback reaches error.log — this
        # blanket guard legitimately covers a missing incident app and DB
        # errors, and without a traceback a programming error in here reads
        # like a transient failure.
        logit.exception("geoip", f"Error checking internal threats for {ip_address}: {e}")
        return {
            'is_known_attacker': False,
            'is_known_abuser': False,
            'internal_stats': {'error': str(e)}
        }


def check_abuseipdb(ip_address):
    """
    Check IP against AbuseIPDB service.
    Free tier: 1,000 checks per day
    """
    config = _get_blocklist_config()['abuseipdb']
    if not config['enabled'] or not config['api_key']:
        return None

    try:
        headers = {
            'Accept': 'application/json',
            'Key': config['api_key']
        }
        params = {
            'ipAddress': ip_address,
            'maxAgeInDays': 90,
            'verbose': ''
        }

        response = requests.get(
            config['url'],
            headers=headers,
            params=params,
            timeout=5
        )

        if response.status_code == 200:
            data = response.json().get('data', {})

            abuse_confidence_score = data.get('abuseConfidenceScore', 0)
            total_reports = data.get('totalReports', 0)

            return {
                'source': 'abuseipdb',
                'is_listed': abuse_confidence_score > 25,  # Configurable threshold
                'confidence_score': abuse_confidence_score,
                'total_reports': total_reports,
                'is_public': data.get('isPublic', True),
                'usage_type': data.get('usageType'),
                'domain': data.get('domain'),
            }
    except Exception as e:
        logit.error("geoip", f"AbuseIPDB check failed for {ip_address}: {e}")

    return None


def check_blocklist_de(ip_address):
    """
    Check IP against blocklist.de — the IPSet-backed cache when it exists
    (refreshed every 6h by the incident app's refresh_threat_lists cron),
    else a live fetch of the list.
    """
    config = _get_blocklist_config()['blocklist_de']
    if not config['enabled']:
        return None

    from .detection import _cached_ip_set
    cached = _cached_ip_set("blocklist_de")
    if cached is not None:
        return {
            'source': 'blocklist.de',
            'is_listed': ip_address in cached
        }

    try:
        response = requests.get(config['url'], timeout=5)
        if response.status_code == 200:
            blocklist = response.text.split('\n')
            is_listed = ip_address in blocklist

            return {
                'source': 'blocklist.de',
                'is_listed': is_listed
            }
    except Exception as e:
        logit.error("geoip", f"Blocklist.de check failed for {ip_address}: {e}")

    return None


def check_all_blocklists(ip_address):
    """
    Check IP against all enabled blocklists.
    Returns aggregated results.
    """
    if not settings.get('GEOLOCATION_ENABLE_BLOCKLIST_CHECK',
                        ENABLE_BLOCKLIST_CHECK, kind="bool"):
        return {
            'blocklist_hits': [],
            'is_blocklisted': False
        }

    results = []

    # Check AbuseIPDB
    abuseipdb_result = check_abuseipdb(ip_address)
    if abuseipdb_result:
        results.append(abuseipdb_result)

    # Check Blocklist.de
    blocklist_de_result = check_blocklist_de(ip_address)
    if blocklist_de_result and blocklist_de_result['is_listed']:
        results.append(blocklist_de_result)

    # Determine if IP is on any blocklist
    is_blocklisted = any(
        result.get('is_listed', False)
        for result in results
    )

    return {
        'blocklist_hits': results,
        'is_blocklisted': is_blocklisted
    }


def perform_threat_check(ip_address, skip_external=False, *,
                         check_internal=None, check_external=None):
    """
    Perform comprehensive threat check on an IP address.
    This is the main entry point for threat intelligence.

    Args:
        ip_address: The IP to check.
        skip_external: When True, skip third-party blocklist HTTP calls and rely
            solely on local internal-event analysis. Used by the `mojo` GeoIP
            provider — the upstream already ran external checks on its side.

    `check_internal` / `check_external` are keyword-only test seams
    (item #2558) defaulting to this module's check_internal_threats and
    check_all_blocklists; production behavior is byte-identical.

    Returns dict with:
    - is_known_attacker: Based on internal high-severity events
    - is_known_abuser: Based on internal abuse patterns
    - is_blocklisted: Listed on external blocklists
    - threat_data: Detailed threat intelligence — `internal` (stats),
      `blocklists` (per-source hits) and `is_blocklisted` (mirror of the
      top-level flag; this is the copy that survives being persisted into
      GeoLocatedIP.data and is what recalculate_threat_level() reads)
    """
    # Check internal incident database
    internal_threats = (check_internal or check_internal_threats)(ip_address)

    # Check external blocklists (unless caller is skipping — e.g. mojo provider
    # where the upstream already aggregated external intel).
    if skip_external:
        blocklist_results = {'blocklist_hits': [], 'is_blocklisted': False}
    else:
        blocklist_results = (check_external or check_all_blocklists)(ip_address)

    # Aggregate results
    result = {
        'is_known_attacker': internal_threats['is_known_attacker'],
        'is_known_abuser': internal_threats['is_known_abuser'],
        'is_blocklisted': blocklist_results['is_blocklisted'],
        'threat_data': {
            'internal': internal_threats['internal_stats'],
            'blocklists': blocklist_results['blocklist_hits'],
            # Consumers persist only this sub-dict (GeoLocatedIP.check_threats,
            # geolocate_ip), and recalculate_threat_level() reads the flag from
            # here — without this copy the +30 blocklist weight can never fire.
            'is_blocklisted': blocklist_results['is_blocklisted'],
        }
    }

    return result


THREAT_LEVEL_ORDER = ('low', 'medium', 'high', 'critical')


def escalate_threat_level(current, candidate):
    """Return the higher of two threat levels — never downgrades.

    Unknown/None values sort below 'low', so a missing level is always
    replaced by a real one.
    """
    def rank(level):
        try:
            return THREAT_LEVEL_ORDER.index(level)
        except ValueError:
            return -1

    return candidate if rank(candidate) > rank(current) else current


def recalculate_threat_level(geo_ip):
    """
    Recalculate threat level based on all available data including
    internal threats and blocklists.

    Duck-typed on purpose: only `.is_known_attacker`, `.is_known_abuser`,
    `.is_tor`, `.is_vpn`, `.is_proxy` and `.data` (a dict) are touched — never
    a manager, field or pk. `geolocate_ip()` calls this with a plain objict
    shim so the helper path and the GeoLocatedIP.check_threats() path score the
    same evidence with one weight table. Keep it that way; a second copy of
    these weights would drift.

    Args:
        geo_ip: GeoLocatedIP instance (or shim) with threat data populated

    Returns:
        str: 'low', 'medium', 'high', or 'critical'
    """
    score = 0

    # Critical threats
    if geo_ip.is_known_attacker:
        score += 50
    if geo_ip.data and geo_ip.data.get('threat_data', {}).get('is_blocklisted'):
        score += 30

    # High threats
    if geo_ip.is_tor:
        score += 40
    if geo_ip.is_known_abuser:
        score += 30

    # Medium threats
    if geo_ip.is_proxy:
        score += 25
    if geo_ip.is_vpn:
        score += 20

    # External threat intelligence
    threat_data = geo_ip.data.get('threat_data', {})
    internal_stats = threat_data.get('internal', {})

    # Boost score based on internal event history
    high_severity_events = internal_stats.get('high_severity_events', 0)
    if high_severity_events > 10:
        score += 20
    elif high_severity_events > 5:
        score += 10

    # Determine threat level
    if score >= 75:
        return 'critical'
    elif score >= 50:
        return 'high'
    elif score >= 25:
        return 'medium'
    else:
        return 'low'
