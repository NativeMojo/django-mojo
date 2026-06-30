import ipaddress
from datetime import timedelta
from django.db import models, IntegrityError
from mojo.helpers.settings import settings
from mojo.models import MojoModel
from mojo.helpers import dates, logit
from mojo.apps import jobs
from mojo.apps import metrics
import ujson


GEOLOCATION_ALLOW_SUBNET_LOOKUP = settings.get_static('GEOLOCATION_ALLOW_SUBNET_LOOKUP', False, kind='bool')
GEOLOCATION_CACHE_DURATION_DAYS = settings.get_static('GEOLOCATION_CACHE_DURATION_DAYS', 90, kind='int')
GEOLOCATION_LAST_SEEN_AGE = settings.get_static('GEOLOCATION_IP_LAST_SEEN_AGE', 300)


class GeoLocatedIP(models.Model, MojoModel):
    """
    Acts as a cache to store geolocation results, reducing redundant and costly API calls.
    Features a standardized, indexed schema for fast querying.

    This model also tracks security-relevant metadata like VPN, Tor, proxy, and cloud platform detection.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True, db_index=True, help_text="Last time this IP was encountered in the system")

    ip_address = models.GenericIPAddressField(db_index=True, unique=True)
    subnet = models.CharField(max_length=45, db_index=True, null=True, default=None)

    # Normalized and indexed fields for querying
    country_code = models.CharField(max_length=3, db_index=True, null=True, blank=True)
    country_name = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
    # ISO 3166-2 subdivision code, e.g. "US-FL". Populated lazily on refresh()
    # from providers that expose it (MaxMind, ip-api, ipstack). For geofence DSL
    # region matching.
    region_code = models.CharField(max_length=10, db_index=True, null=True, blank=True)
    city = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=50, null=True, blank=True)

    # Security and anonymity detection
    is_tor = models.BooleanField(default=False, db_index=True, help_text="Is this IP a known Tor exit node?")
    is_vpn = models.BooleanField(default=False, db_index=True, help_text="Is this IP associated with a VPN service?")
    is_proxy = models.BooleanField(default=False, db_index=True, help_text="Is this IP a known proxy server?")
    is_cloud = models.BooleanField(default=False, db_index=True, help_text="Is this IP from a cloud platform (AWS, GCP, Azure, etc.)?")
    is_datacenter = models.BooleanField(default=False, db_index=True, help_text="Is this IP from a datacenter/hosting provider?")
    is_mobile = models.BooleanField(default=False, db_index=True, help_text="Is this IP from a mobile/cellular carrier?")
    is_known_attacker = models.BooleanField(default=False, db_index=True, help_text="Is this IP a known attacker?")
    is_known_abuser = models.BooleanField(default=False, db_index=True, help_text="Is this IP a known abuser?")

    # Additional security metadata
    threat_level = models.CharField(
        max_length=20,
        db_index=True,
        null=True,
        blank=True,
        help_text="Threat level: low, medium, high, critical"
    )
    asn = models.CharField(max_length=50, null=True, blank=True, help_text="Autonomous System Number")
    asn_org = models.CharField(max_length=255, null=True, blank=True, help_text="Organization owning the ASN")
    isp = models.CharField(max_length=255, null=True, blank=True, help_text="Internet Service Provider")
    mobile_carrier = models.CharField(max_length=100, null=True, blank=True, db_index=True, help_text="Mobile carrier name (Verizon, AT&T, T-Mobile, etc.)")
    connection_type = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Connection type: residential, business, hosting, cellular, etc."
    )

    # Incident-driven blocking
    is_blocked = models.BooleanField(default=False, db_index=True, help_text="Is this IP currently blocked?")
    blocked_at = models.DateTimeField(null=True, blank=True, help_text="When this IP was blocked")
    blocked_until = models.DateTimeField(null=True, blank=True, db_index=True, help_text="When the block expires (null = permanent)")
    blocked_reason = models.CharField(max_length=255, null=True, blank=True, help_text="Why this IP was blocked")
    block_count = models.IntegerField(default=0, help_text="Number of times this IP has been blocked")

    # Whitelisting — takes precedence over all blocking
    is_whitelisted = models.BooleanField(default=False, db_index=True, help_text="Whitelisted IPs are never blocked")
    whitelisted_reason = models.CharField(max_length=255, null=True, blank=True, help_text="Why this IP is whitelisted")

    # Auditing and source tracking
    provider = models.CharField(max_length=50, null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(default=None, null=True, blank=True)

    class Meta:
        verbose_name = "Geolocated IP"
        verbose_name_plural = "Geolocated IPs"
        indexes = [
            models.Index(fields=['is_tor', 'is_vpn', 'is_proxy']),
            models.Index(fields=['threat_level', 'modified']),
            models.Index(fields=['is_cloud', 'is_datacenter']),
            models.Index(fields=['is_mobile', 'mobile_carrier']),
            models.Index(fields=['is_blocked', 'threat_level']),
            models.Index(fields=['is_blocked', 'blocked_until']),
            models.Index(fields=['is_whitelisted']),
        ]

    class RestMeta:
        VIEW_PERMS = ['manage_users', 'view_security', 'manage_security', 'security', 'users']
        SAVE_PERMS = ['manage_users', 'manage_security', 'security']
        SEARCH_FIELDS = ["ip_address", "city", "country_name", "asn_org", "isp"]
        POST_SAVE_ACTIONS = ["refresh", "threat_analysis", "block", "unblock", "whitelist", "unwhitelist"]
        GRAPHS = {
            'default': {
                'extra': ['is_threat', 'is_suspicious', 'risk_score'],
                'exclude': ['data', 'provider']
            },
            'basic': {
                'fields': ['id', 'ip_address', 'country_code', 'country_name', 'city', 'region',
                           'is_tor', 'is_vpn', 'is_proxy', 'is_known_attacker', 'is_known_abuser',
                           'threat_level', 'is_blocked', 'blocked_at', 'blocked_until',
                           'blocked_reason', 'block_count', 'is_whitelisted', 'whitelisted_reason'],
                'extra': ['is_threat', 'is_suspicious', 'risk_score', 'block_active'],
            },
            'detailed': {
                # Include all fields including raw data
                'extra': ['is_threat', 'is_suspicious', 'risk_score']
            },
            'federation': {
                # Returned to other mojo instances using this one as their
                # GeoIP provider. Contains only abuse-signal and location
                # fields — NEVER per-fleet firewall state (is_blocked,
                # is_whitelisted, blocked_*, whitelisted_*) and NEVER the raw
                # provider data blob (which can also carry firewall hints).
                'fields': [
                    'id', 'ip_address',
                    'country_code', 'country_name', 'region', 'region_code',
                    'city', 'postal_code', 'latitude', 'longitude', 'timezone',
                    'asn', 'asn_org', 'isp', 'connection_type', 'mobile_carrier',
                    'is_tor', 'is_vpn', 'is_proxy', 'is_cloud',
                    'is_datacenter', 'is_mobile',
                    'is_known_attacker', 'is_known_abuser', 'threat_level',
                    'provider',
                ],
            },
        }

    def __str__(self):
        flags = []
        if self.is_tor:
            flags.append("TOR")
        if self.is_vpn:
            flags.append("VPN")
        if self.is_proxy:
            flags.append("PROXY")
        if self.is_cloud:
            flags.append("CLOUD")
        if self.is_mobile:
            carrier = self.mobile_carrier or "MOBILE"
            flags.append(carrier)

        flag_str = f" [{', '.join(flags)}]" if flags else ""
        return f"{self.ip_address} ({self.city}, {self.country_code}){flag_str}"

    @property
    def block_active(self):
        """True if the IP is blocked AND the block hasn't expired."""
        if not self.is_blocked:
            return False
        if self.is_whitelisted:
            return False
        if self.blocked_until and dates.utcnow() > self.blocked_until:
            return False
        return True

    @property
    def is_expired(self):
        if self.provider == 'internal':
            return False  # Internal records never expire
        if self.expires_at:
            return dates.utcnow() > self.expires_at
        return True  # If no expiry is set, it needs a refresh

    @property
    def is_threat(self):
        return self.is_known_attacker or self.is_known_abuser

    @property
    def is_suspicious(self):
        """
        Returns True if this IP has any suspicious characteristics.
        """
        return any([
            self.is_tor,
            self.is_vpn,
            self.is_proxy,
            self.threat_level in ['high', 'critical']
        ])

    @property
    def risk_score(self):
        """
        Calculate a simple risk score from 0-100 based on various factors.
        """
        score = 0

        if self.is_tor:
            score += 40
        if self.is_vpn:
            score += 20
        if self.is_proxy:
            score += 25
        if self.threat_level == 'critical':
            score += 30
        elif self.threat_level == 'high':
            score += 20
        elif self.threat_level == 'medium':
            score += 10

        # Cap at 100
        return min(score, 100)

    def refresh(self, check_threats=False):
        """
        Refreshes the geolocation data for this IP by calling the geolocation
        helper and updating the model instance with the returned data.

        Args:
            check_threats: If True, also perform threat intelligence checks
        """
        from mojo.helpers import geoip

        geo_data = geoip.geolocate_ip(self.ip_address, check_threats=check_threats)

        if not geo_data or not geo_data.get("provider"):
            return False

        # Update self with new data
        for key, value in geo_data.items():
            if hasattr(self, key):
                setattr(self, key, value)

        # Set the expiration date
        if self.provider == 'internal':
            self.expires_at = None
        else:
            cache_duration_days = GEOLOCATION_CACHE_DURATION_DAYS
            self.expires_at = dates.utcnow() + timedelta(days=cache_duration_days)

        self.save()
        return True

    def check_threats(self, from_sync=False):
        """
        Perform comprehensive threat intelligence checks on this IP.
        Updates is_known_attacker, is_known_abuser, and threat_level fields.
        Stores detailed threat data in the data JSON field.

        This can be called independently or as part of refresh().

        Args:
            from_sync: When True, suppress outbound push-back to the upstream
                mojo provider (the trigger arrived via the federation endpoint).
        """
        from mojo.helpers.geoip import threat_intel

        prev_snapshot = self._abuse_snapshot()

        # For records sourced from a mojo upstream, the upstream already ran
        # external blocklist checks — only re-run the local internal-events
        # analysis here. OR-merge with existing values; never downgrade.
        if self.provider == 'mojo':
            threat_results = threat_intel.perform_threat_check(
                self.ip_address, skip_external=True
            )
            self.is_known_attacker = bool(
                self.is_known_attacker or threat_results['is_known_attacker']
            )
            self.is_known_abuser = bool(
                self.is_known_abuser or threat_results['is_known_abuser']
            )
        else:
            threat_results = threat_intel.perform_threat_check(self.ip_address)
            self.is_known_attacker = threat_results['is_known_attacker']
            self.is_known_abuser = threat_results['is_known_abuser']

        # Store detailed threat data
        if not self.data:
            self.data = {}
        self.data['threat_data'] = threat_results['threat_data']
        self.data['threat_checked_at'] = dates.utcnow().isoformat()

        # Recalculate threat level with new data — but never downgrade for
        # mojo-sourced records (upstream may have set it higher than our local
        # signals would).
        recalculated = threat_intel.recalculate_threat_level(self)
        if self.provider == 'mojo':
            order = self.THREAT_LEVEL_ORDER
            cur_idx = order.index(self.threat_level) if self.threat_level in order else 0
            new_idx = order.index(recalculated) if recalculated in order else 0
            if new_idx > cur_idx:
                self.threat_level = recalculated
        else:
            self.threat_level = recalculated

        self.save()

        if not from_sync:
            self._maybe_push_abuse_signals(prev_snapshot)

        return threat_results

    THREAT_LEVEL_ORDER = [None, 'low', 'medium', 'high', 'critical']

    def _abuse_snapshot(self):
        """Capture the federated abuse-signal fields prior to a mutation."""
        return {
            'threat_level': self.threat_level,
            'is_known_attacker': bool(self.is_known_attacker),
            'is_known_abuser': bool(self.is_known_abuser),
        }

    def _maybe_push_abuse_signals(self, prev_snapshot):
        """
        Enqueue a push-back job to the upstream mojo provider when an abuse
        signal strictly rises on a record sourced from the `mojo` provider.

        Push-back is always async (jobs.publish) — callers must never block on
        the HTTP POST. Failure to enqueue is logged and swallowed; local state
        is never affected.

        Federation rules:
          - threat_level: pushed when the level strictly rises.
          - is_known_attacker / is_known_abuser: pushed on False→True flips.

        Gated on:
          - self.provider == 'mojo'  (only federate records we pulled from a
                                      mojo upstream)
          - GEOIP_MOJO_PROVIDER_URL configured
          - GEOIP_MOJO_SYNC_ENABLED True (master kill switch)
        """
        try:
            if self.provider != 'mojo':
                return
            from mojo.helpers.geoip import config as geoip_config
            if not geoip_config.MOJO_SYNC_ENABLED:
                return
            if not geoip_config.MOJO_PROVIDER_URL:
                return

            order = self.THREAT_LEVEL_ORDER
            prev_level = prev_snapshot.get('threat_level')
            prev_idx = order.index(prev_level) if prev_level in order else 0
            cur_idx = order.index(self.threat_level) if self.threat_level in order else 0

            changed = {}
            if cur_idx > prev_idx and self.threat_level is not None:
                changed['threat_level'] = self.threat_level
            if self.is_known_attacker and not prev_snapshot.get('is_known_attacker'):
                changed['is_known_attacker'] = True
            if self.is_known_abuser and not prev_snapshot.get('is_known_abuser'):
                changed['is_known_abuser'] = True

            if not changed:
                return

            # Build a deterministic idempotency key from the changed-fields
            # summary so retries dedupe but distinct upward transitions don't.
            summary_parts = []
            if 'threat_level' in changed:
                summary_parts.append(f"level={changed['threat_level']}")
            if 'is_known_attacker' in changed:
                summary_parts.append("attacker=1")
            if 'is_known_abuser' in changed:
                summary_parts.append("abuser=1")
            idempotency_key = f"geoip_sync:{self.ip_address}:{'|'.join(summary_parts)}"

            payload = {"ip": self.ip_address, **changed}
            jobs.publish(
                "mojo.apps.account.asyncjobs.push_abuse_signals",
                payload,
                idempotency_key=idempotency_key,
                max_retries=5,
            )
        except Exception:
            logit.exception(
                "Failed to enqueue geoip push-back for %s", self.ip_address
            )

    def update_threat_from_incident(self, priority, block=False, from_sync=False):
        """
        Called when a new incident is created for this IP.
        Escalates threat_level based on incident priority (0-15 scale).
        Never downgrades. Whitelisted IPs are never auto-blocked.

        Args:
            priority: Incident priority (0-15 scale).
            block: When True, auto-block on high/critical escalations.
            from_sync: When True, suppress outbound push-back (the change
                arrived via the federation sync endpoint).
        """
        if priority >= 13:
            new_level = 'critical'
        elif priority >= 10:
            new_level = 'high'
        elif priority >= 7:
            new_level = 'medium'
        else:
            return  # Below threshold, nothing to do

        current_idx = self.THREAT_LEVEL_ORDER.index(self.threat_level) if self.threat_level in self.THREAT_LEVEL_ORDER else 0
        new_idx = self.THREAT_LEVEL_ORDER.index(new_level)

        if new_idx <= current_idx:
            return  # Already at this level or higher, no change needed

        prev = self._abuse_snapshot()
        self.threat_level = new_level
        update_fields = ['threat_level']

        # Never auto-block whitelisted IPs
        if self.is_whitelisted or not block:
            self.save(update_fields=update_fields)
            if not from_sync:
                self._maybe_push_abuse_signals(prev)
            return

        # Auto-block when threat reaches high/critical
        if not self.is_blocked and new_level in ('high', 'critical'):
            self.block('auto:threat_escalation', ttl=900, from_sync=from_sync)
        self.save(update_fields=update_fields)
        if not from_sync:
            self._maybe_push_abuse_signals(prev)

    def block(self, reason="manual", ttl=None, broadcast=True, from_sync=False):
        """
        Block this IP fleet-wide. Updates the database AND broadcasts
        the block to all instances via the job system.

        block() always ensures threat_level is at least 'high' — blocking is a
        strong abuse signal, and centralizing the escalation here lets every
        block entry point (admin REST, LLM agent, rule engine, asyncjobs)
        feed the federation loop without changes.

        Args:
            reason: Why the IP is being blocked
            ttl: Seconds until auto-unblock (None = permanent)
            broadcast: If True, broadcast to all instances (set False for DB-only updates)
            from_sync: When True, suppress outbound push-back to the upstream
                mojo provider (the change arrived via the sync endpoint).
        Returns:
            bool: True if the IP was blocked
        """
        if self.is_whitelisted:
            return False

        # Idempotency: skip if already blocked and block hasn't expired
        if self.is_blocked and self.block_active:
            return True

        prev_snapshot = self._abuse_snapshot()

        # Escalate threat_level to at least 'high' as part of the same atomic
        # update — block is a strong abuse signal. Never downgrade.
        order = self.THREAT_LEVEL_ORDER
        cur_idx = order.index(self.threat_level) if self.threat_level in order else 0
        high_idx = order.index('high')
        new_threat_level = self.threat_level if cur_idx >= high_idx else 'high'

        # Atomic conditional update to prevent concurrent workers from
        # double-blocking the same IP (race-safe idempotency).
        # Match IPs that are either not blocked or have an expired block.
        now = dates.utcnow()
        ttl = int(ttl) if ttl else 0
        blocked_until = now + timedelta(seconds=ttl) if ttl else None
        updated = GeoLocatedIP.objects.filter(
            pk=self.pk,
        ).filter(
            models.Q(is_blocked=False) | models.Q(blocked_until__lte=now),
        ).update(
            is_blocked=True,
            blocked_at=now,
            blocked_reason=reason,
            block_count=models.F('block_count') + 1,
            blocked_until=blocked_until,
            threat_level=new_threat_level,
        )
        if not updated:
            # Already actively blocked (by us or a concurrent worker)
            self.refresh_from_db()
            return True

        self.refresh_from_db()

        if not from_sync:
            self._maybe_push_abuse_signals(prev_snapshot)

        # Structured logit entry
        trigger = "auto:incident_rule" if reason == "auto:ruleset" else "manual"
        self.log(
            f"IP Blocked: {self.ip_address} - {reason}",
            "firewall:block",
            payload=ujson.dumps({
                "ip": self.ip_address,
                "reason": reason,
                "ttl": ttl or None,
                "blocked_until": str(self.blocked_until) if self.blocked_until else None,
                "block_count": self.block_count,
                "trigger": trigger,
            }),
        )

        # Metrics — only for blocks
        metrics.record("firewall:blocks", category="firewall")
        if self.country_code:
            metrics.record(f"firewall:blocks:country:{self.country_code}", category="firewall")

        if broadcast:
            try:
                if not ttl:
                    # Permanent block — route through ipset for O(1) lookup
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_ipset_add_blocked",
                        {"ip": self.ip_address},
                    )
                else:
                    # TTL block — individual iptables rule (expires soon)
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_block_ip",
                        {"ips": [self.ip_address], "ttl": ttl},
                    )
                metrics.record("firewall:broadcasts", category="firewall")
            except Exception:
                logit.exception("Failed to broadcast block for %s", self.ip_address)
                metrics.record("firewall:broadcast_errors", category="firewall")

        return True

    def unblock(self, reason="manual", broadcast=True):
        """
        Unblock this IP fleet-wide. Updates the database AND broadcasts
        the unblock to all instances.
        """
        # Read DB truth to avoid stale in-memory state from concurrent updates
        db_state = GeoLocatedIP.objects.filter(pk=self.pk).values("is_blocked", "blocked_until").first()
        was_permanent = db_state and db_state["is_blocked"] and db_state["blocked_until"] is None

        self.is_blocked = False
        self.blocked_reason = f"unblocked: {reason}"
        self.blocked_until = None
        self.save(update_fields=['is_blocked', 'blocked_reason', 'blocked_until'])

        self.log(
            f"IP Unblocked: {self.ip_address} - {reason}",
            "firewall:unblock",
            payload=ujson.dumps({
                "ip": self.ip_address,
                "reason": reason,
                "trigger": "manual",
            }),
        )

        if broadcast:
            try:
                if was_permanent:
                    # Remove from ipset (permanent blocks live in mojo_blocked)
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_ipset_del_blocked",
                        {"ip": self.ip_address},
                    )
                else:
                    # Remove individual iptables rule (TTL blocks)
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_unblock_ip",
                        {"ips": [self.ip_address]},
                    )
            except Exception:
                logit.exception("Failed to broadcast unblock for %s", self.ip_address)
                metrics.record("firewall:broadcast_errors", category="firewall")

    def whitelist(self, reason="manual"):
        """
        Whitelist this IP. Unblocks fleet-wide if currently blocked
        and prevents future auto-blocks.
        """
        # Read DB truth to avoid stale in-memory state from concurrent updates
        db_state = GeoLocatedIP.objects.filter(pk=self.pk).values("is_blocked", "blocked_until").first()
        was_blocked = bool(db_state and db_state["is_blocked"])
        was_permanent = was_blocked and db_state["blocked_until"] is None

        self.is_whitelisted = True
        self.whitelisted_reason = reason
        if self.is_blocked:
            self.is_blocked = False
            self.blocked_until = None
        self.save(update_fields=[
            'is_whitelisted', 'whitelisted_reason',
            'is_blocked', 'blocked_until',
        ])

        self.log(
            f"IP Whitelisted: {self.ip_address} - {reason}",
            "firewall:whitelist",
            payload=ujson.dumps({
                "ip": self.ip_address,
                "reason": reason,
                "was_blocked": was_blocked,
                "trigger": "manual",
            }),
        )

        if was_blocked:
            try:
                if was_permanent:
                    # Remove from ipset (permanent blocks live in mojo_blocked)
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_ipset_del_blocked",
                        {"ip": self.ip_address},
                    )
                else:
                    # Remove individual iptables rule (TTL blocks)
                    jobs.broadcast_execute(
                        "mojo.apps.incident.asyncjobs.broadcast_unblock_ip",
                        {"ips": [self.ip_address]},
                    )
            except Exception:
                logit.exception("Failed to broadcast unblock for %s", self.ip_address)
                metrics.record("firewall:broadcast_errors", category="firewall")

    def unwhitelist(self):
        """Remove whitelist status."""
        self.is_whitelisted = False
        self.whitelisted_reason = None
        self.save(update_fields=['is_whitelisted', 'whitelisted_reason'])

        self.log(
            f"IP Unwhitelisted: {self.ip_address}",
            "firewall:unwhitelist",
            payload=ujson.dumps({
                "ip": self.ip_address,
                "trigger": "manual",
            }),
        )

    def on_action_block(self, value):
        if not isinstance(value, dict):
            username = self.active_user.username if self.active_user else "unknown"
            value = {"reason": f"manual block: by {username}", "ttl": 600}
        self.block(reason=value.get("reason", ""), ttl=value.get("ttl"))

    def on_action_unblock(self, value):
        if not isinstance(value, str):
            username = self.active_user.username if self.active_user else "unknown"
            value = f"manual unblock: by {username}"
        self.unblock(reason=value)

    def on_action_whitelist(self, value):
        if not isinstance(value, str):
            username = self.active_user.username if self.active_user else "unknown"
            value = f"manual whitelist: by {username}"
        self.whitelist(reason=value)

    def on_action_unwhitelist(self, value):
        self.unwhitelist()

    def on_action_refresh(self, value):
        self.refresh(check_threats=True)

    def on_action_threat_analysis(self, value):
        self.check_threats()

    @classmethod
    def lookup(cls, ip_address, auto_refresh=True, subdomain_only=GEOLOCATION_ALLOW_SUBNET_LOOKUP):
        return cls.geolocate(ip_address, auto_refresh, subdomain_only)

    @classmethod
    def geolocate(cls, ip_address, auto_refresh=True, subdomain_only=GEOLOCATION_ALLOW_SUBNET_LOOKUP):
        """
        Get or create a GeoLocatedIP record for the given IP address.

        Args:
            ip_address: The IP address to geolocate
            auto_refresh: If True, refresh expired records immediately
            subdomain_only: If True, only look up subnet matches

        Returns:
            GeoLocatedIP instance
        """
        # Extract subnet from the IP. IPv4 keeps the historical string prefix (so existing
        # subnet_match rows still match); IPv6 uses the /64 network (rfind('.') doesn't apply).
        if ':' in ip_address:
            try:
                subnet = str(ipaddress.ip_network(f"{ip_address}/64", strict=False).network_address)
            except ValueError:
                subnet = ip_address
        else:
            subnet = ip_address[:ip_address.rfind('.')]
        geo_ip = cls.objects.filter(ip_address=ip_address).first()

        if not geo_ip and subdomain_only:
            subnet_match = cls.objects.filter(subnet=subnet).last()
            if subnet_match:
                provider = subnet_match.provider
                if provider and "subnet" not in provider:
                    provider = f"subnet:{provider}"
                try:
                    geo_ip = cls.objects.create(
                        ip_address=ip_address, subnet=subnet,
                        city=subnet_match.city, region=subnet_match.region,
                        country_name=subnet_match.country_name, country_code=subnet_match.country_code,
                        latitude=subnet_match.latitude, longitude=subnet_match.longitude,
                        timezone=subnet_match.timezone, provider=provider,
                    )
                except IntegrityError:
                    geo_ip = cls.objects.filter(ip_address=ip_address).first()

        if not geo_ip:
            try:
                geo_ip = cls.objects.create(ip_address=ip_address, subnet=subnet)
            except IntegrityError:
                geo_ip = cls.objects.filter(ip_address=ip_address).first()
        else:
            # Touch last_seen only when stale to avoid unnecessary writes
            age_seconds = (dates.utcnow() - geo_ip.last_seen).total_seconds()
            if age_seconds > GEOLOCATION_LAST_SEEN_AGE:
                geo_ip.last_seen = dates.utcnow()
                geo_ip.save(update_fields=['last_seen'])

        if auto_refresh and geo_ip.is_expired:
            geo_ip.refresh()

        return geo_ip
