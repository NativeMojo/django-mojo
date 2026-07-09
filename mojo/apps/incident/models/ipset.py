from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates, logit


KIND_CHOICES = [
    ("country", "Country"),
    ("datacenter", "Datacenter"),
    ("abuse", "Abuse List"),
    ("custom", "Custom"),
]

SOURCE_CHOICES = [
    ("ipdeny", "ipdeny.com"),
    ("abuseipdb", "AbuseIPDB"),
    ("tor", "Tor Exit List"),
    ("blocklist_de", "blocklist.de"),
    ("manual", "Manual"),
]

BLOCKLIST_DE_URL = "https://lists.blocklist.de/lists/all.txt"


def _parse_tor_exit_list(text):
    """Extract exit-node IPs from the Tor Project exit list.

    Format: blocks of metadata lines; the address lines look like
    `ExitAddress 1.2.3.4 2026-07-08 12:00:00`.
    """
    ips = []
    for line in text.splitlines():
        if line.startswith("ExitAddress "):
            parts = line.split()
            if len(parts) >= 2:
                ips.append(parts[1])
    return ips


class IPSet(models.Model, MojoModel):
    """
    Manages ipset-based bulk IP blocking (countries, datacenters, abuse lists).

    Each record represents one ipset (e.g. "country_cn", "abuse_ips", "azure").
    The CIDR data is stored directly in the `data` TextField — no external
    file dependency at sync time.

    The sync action broadcasts to all instances so every EC2 behind the
    load balancer gets the same ipset rules.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    name = models.CharField(max_length=100, unique=True, help_text="ipset name, e.g. country_cn, abuse_ips, azure")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    description = models.CharField(max_length=255, null=True, blank=True)

    # Where the data comes from (for auto-refresh)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")
    source_url = models.CharField(max_length=500, null=True, blank=True, help_text="URL to fetch CIDR list from (ipdeny, etc.)")
    source_key = models.CharField(max_length=255, null=True, blank=True, help_text="API key or identifier for the source")

    # The actual CIDR data — one CIDR per line
    data = models.TextField(default="", blank=True, help_text="CIDR list, one per line")

    is_enabled = models.BooleanField(default=True, db_index=True)
    cidr_count = models.IntegerField(default=0)
    last_synced = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "IP Set"
        verbose_name_plural = "IP Sets"

    class RestMeta:
        VIEW_PERMS = ["view_security", "security"]
        SAVE_PERMS = ["manage_security", "security"]
        DELETE_PERMS = ["manage_security"]
        SEARCH_FIELDS = ["name", "description"]
        POST_SAVE_ACTIONS = ["sync", "enable", "disable", "refresh_source"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "exclude": ["data", "source_key"],
            },
            "detailed": {
                "exclude": ["source_key"],
            },
        }

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.name} ({self.kind}, {self.cidr_count} CIDRs, {status})"

    @property
    def is_cache_only(self):
        """True for the geoip threat-list caches — rows that must never be
        synced into the kernel firewall (see THREAT_CACHE_SETS)."""
        return self.name in self.THREAT_CACHE_SETS

    @property
    def cidrs(self):
        """Returns the CIDR list as a list of strings."""
        if not self.data:
            return []
        return [line.strip() for line in self.data.strip().splitlines() if line.strip() and not line.startswith("#")]

    def set_data(self, cidr_list):
        """Set CIDR data from a list of strings."""
        self.data = "\n".join(cidr_list)
        self.cidr_count = len(cidr_list)

    def on_action_sync(self, value):
        """Broadcast ipset to all instances."""
        self.sync()

    def on_action_enable(self, value):
        if self.is_cache_only:
            from mojo import errors as merrors
            raise merrors.ValueException(
                f"'{self.name}' is a cache-only threat list for geoip "
                f"detection — enabling it would kernel-block every listed IP "
                f"fleet-wide and is not permitted")
        self.is_enabled = True
        self.save(update_fields=["is_enabled"])
        self.sync()

    def on_action_disable(self, value):
        """Disable and remove ipset from all instances."""
        self.is_enabled = False
        self.save(update_fields=["is_enabled"])
        from mojo.apps import jobs
        jobs.broadcast_execute(
            "mojo.apps.incident.asyncjobs.broadcast_remove_ipset",
            {"name": self.name},
        )

    def on_action_refresh_source(self, value):
        """Fetch latest data from source, then sync to all instances."""
        if self.refresh_from_source():
            self.sync()

    def sync(self):
        """Broadcast this ipset to all instances."""
        # Hard circuit breaker: the cache-only threat lists must never reach
        # the kernel firewall, even if is_enabled was force-set via a generic
        # field save (the enable action also rejects them with a 400).
        if not self.is_enabled or self.is_cache_only:
            return
        from mojo.apps import jobs
        jobs.broadcast_execute(
            "mojo.apps.incident.asyncjobs.broadcast_sync_ipset",
            {"name": self.name, "cidrs": self.cidrs},
        )
        self.last_synced = dates.utcnow()
        self.sync_error = None
        self.save(update_fields=["last_synced", "sync_error"])

    def refresh(self):
        """Refresh the ipset from the source and sync."""
        if self.refresh_from_source():
            self.sync()

    def refresh_from_source(self):
        """Fetch latest CIDR data from the configured source."""
        if self.source == "manual":
            return False

        try:
            if self.source == "ipdeny":
                data = self._fetch_ipdeny()
            elif self.source == "abuseipdb":
                data = self._fetch_abuseipdb()
            elif self.source == "tor":
                data = self._fetch_tor()
            elif self.source == "blocklist_de":
                data = self._fetch_blocklist_de()
            else:
                return False

            if data:
                self.set_data(data)
                self.sync_error = None
                self.save(update_fields=["data", "cidr_count", "sync_error"])
                return True
            return False
        except Exception as e:
            self.sync_error = str(e)
            self.save(update_fields=["sync_error"])
            logit.error(f"IPSet refresh failed for {self.name}: {e}")
            return False

    def _fetch_ipdeny(self):
        """Fetch country zone file from ipdeny.com."""
        import requests
        if not self.source_url:
            if not self.name or not self.name.startswith("country_"):
                raise ValueError(
                    f"IPSet '{self.name}' has source=ipdeny but no source_url and "
                    f"name does not start with 'country_' — cannot derive URL"
                )
            import re
            code = self.name[len("country_"):]
            if not re.fullmatch(r'[a-z]{2}', code):
                raise ValueError(
                    f"IPSet '{self.name}': derived country code '{code}' is not a valid "
                    f"2-letter code — cannot construct ipdeny URL"
                )
            self.source_url = f"https://www.ipdeny.com/ipblocks/data/countries/{code}.zone"
            self.save(update_fields=["source_url"])
        resp = requests.get(self.source_url, timeout=30)
        resp.raise_for_status()
        lines = [line.strip() for line in resp.text.splitlines() if line.strip() and not line.startswith("#")]
        return lines

    def _fetch_abuseipdb(self):
        """Fetch abuse IP blacklist from AbuseIPDB API."""
        import requests
        if not self.source_key:
            return None
        resp = requests.get(
            "https://api.abuseipdb.com/api/v2/blacklist",
            headers={"Key": self.source_key, "Accept": "text/plain"},
            params={"confidenceMinimum": 100, "limit": 10000, "ipVersion": 4, "plaintext": ""},
            timeout=30,
        )
        resp.raise_for_status()
        lines = [line.strip() for line in resp.text.splitlines() if line.strip()]
        return lines

    def _fetch_tor(self):
        """Fetch the Tor Project exit-node list (ExitAddress lines → bare IPs)."""
        import requests
        url = self.source_url
        if not url:
            from mojo.helpers.geoip.config import TOR_EXIT_NODE_LIST_URL
            url = TOR_EXIT_NODE_LIST_URL
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return _parse_tor_exit_list(resp.text)

    def _fetch_blocklist_de(self):
        """Fetch the blocklist.de aggregate list (one IP per line)."""
        import requests
        resp = requests.get(self.source_url or BLOCKLIST_DE_URL, timeout=30)
        resp.raise_for_status()
        return [line.strip() for line in resp.text.splitlines()
                if line.strip() and not line.startswith("#")]

    # Cache-only threat lists consumed by mojo.helpers.geoip detection —
    # refreshed by the refresh_threat_lists cron via refresh_from_source()
    # ONLY. is_enabled stays False so they are excluded from the weekly
    # refresh_ipsets cron and sync() (kernel firewall) stays a no-op.
    THREAT_CACHE_SETS = {
        "tor_exits": {"kind": "abuse", "source": "tor"},
        "blocklist_de": {"kind": "abuse", "source": "blocklist_de"},
    }

    @classmethod
    def ensure_threat_caches(cls):
        """Idempotently create the cache-only threat-list rows (disabled)."""
        rows = []
        for name, defaults in cls.THREAT_CACHE_SETS.items():
            row, _ = cls.objects.get_or_create(
                name=name,
                defaults={
                    **defaults,
                    "is_enabled": False,
                    "description": (
                        "Cache-only list for geoip detection — do NOT enable; "
                        "enabling would kernel-block every listed IP fleet-wide."
                    ),
                },
            )
            rows.append(row)
        return rows

    @classmethod
    def create_country(cls, country_code, enabled=True):
        """Helper to create a country IPSet."""
        code = country_code.lower()
        return cls.objects.update_or_create(
            name=f"country_{code}",
            defaults={
                "kind": "country",
                "description": f"Block country: {code.upper()}",
                "source": "ipdeny",
                "source_url": f"https://www.ipdeny.com/ipblocks/data/countries/{code}.zone",
                "is_enabled": enabled,
            }
        )[0]

    @classmethod
    def create_abuse_list(cls, api_key, enabled=True):
        """Helper to create an AbuseIPDB IPSet."""
        return cls.objects.update_or_create(
            name="abuse_ips",
            defaults={
                "kind": "abuse",
                "description": "AbuseIPDB blacklist (confidence 100%)",
                "source": "abuseipdb",
                "source_key": api_key,
                "is_enabled": enabled,
            }
        )[0]
