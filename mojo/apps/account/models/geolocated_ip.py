from datetime import timedelta
from django.db import models
from mojo.helpers.settings import settings
from mojo.models import MojoModel
from mojo.helpers import dates
from mojo.apps import jobs

GEOLOCATION_ALLOW_SUBNET_LOOKUP = settings.get('GEOLOCATION_ALLOW_SUBNET_LOOKUP', False)
GEOLOCATION_CACHE_DURATION_DAYS = settings.get('GEOLOCATION_CACHE_DURATION_DAYS', 90)


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
    subnet = models.CharField(max_length=16, db_index=True, null=True, default=None)

    # Normalized and indexed fields for querying
    country_code = models.CharField(max_length=3, db_index=True, null=True, blank=True)
    country_name = models.CharField(max_length=100, null=True, blank=True)
    region = models.CharField(max_length=100, db_index=True, null=True, blank=True)
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
        ]

    class RestMeta:
        VIEW_PERMS = ['manage_users']
        SEARCH_FIELDS = ["ip_address", "city", "country_name", "asn_org", "isp"]
        POST_SAVE_ACTIONS = ["refresh", "threat_analysis"],
        GRAPHS = {
            'default': {
                'extra': ['is_threat', 'is_suspicious', 'risk_score'],
                'exclude': ['data', 'provider']
            },
            'detailed': {
                # Include all fields including raw data
                'extra': ['is_threat', 'is_suspicious', 'risk_score']
            }
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

    def check_threats(self):
        """
        Perform comprehensive threat intelligence checks on this IP.
        Updates is_known_attacker, is_known_abuser, and threat_level fields.
        Stores detailed threat data in the data JSON field.

        This can be called independently or as part of refresh().
        """
        from mojo.helpers.geoip import threat_intel

        threat_results = threat_intel.perform_threat_check(self.ip_address)

        # Update threat flags
        self.is_known_attacker = threat_results['is_known_attacker']
        self.is_known_abuser = threat_results['is_known_abuser']

        # Store detailed threat data
        if not self.data:
            self.data = {}
        self.data['threat_data'] = threat_results['threat_data']
        self.data['threat_checked_at'] = dates.utcnow().isoformat()

        # Recalculate threat level with new data
        self.threat_level = threat_intel.recalculate_threat_level(self)

        self.save()
        return threat_results

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
        # Extract subnet from IP address using simple string parsing
        subnet = ip_address[:ip_address.rfind('.')]
        geo_ip = cls.objects.filter(ip_address=ip_address).first()

        if not geo_ip and subdomain_only:
            geo_ip = cls.objects.filter(subnet=subnet).last()
            if geo_ip:
                geo_ip.id = None
                geo_ip.pk = None
                geo_ip.ip_address = ip_address
                if geo_ip.provider and "subnet" not in geo_ip.provider:
                    geo_ip.provider = f"subnet:{geo_ip.provider}"
                geo_ip.save()

        if not geo_ip:
            geo_ip = cls.objects.create(ip_address=ip_address, subnet=subnet)
        else:
            # Touch last_seen to track when this IP was last encountered
            geo_ip.last_seen = dates.utcnow()
            geo_ip.save(update_fields=['last_seen'])

        if auto_refresh and geo_ip.is_expired:
            geo_ip.refresh()

        return geo_ip
