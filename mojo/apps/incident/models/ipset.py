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
    ("manual", "Manual"),
]


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
        VIEW_PERMS = ["manage_users"]
        SEARCH_FIELDS = ["name", "description"]
        POST_SAVE_ACTIONS = ["sync", "enable", "disable", "refresh_source"]
        CAN_DELETE = True
        GRAPHS = {
            "default": {
                "exclude": ["data"],
            },
            "detailed": {},
        }

    def __str__(self):
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.name} ({self.kind}, {self.cidr_count} CIDRs, {status})"

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
        self.is_enabled = True
        self.save(update_fields=["is_enabled"])
        self.sync()

    def on_action_disable(self, value):
        """Disable and remove ipset from all instances."""
        self.is_enabled = False
        self.save(update_fields=["is_enabled"])
        from mojo.apps import jobs
        jobs.broadcast_execute(
            "mojo.apps.incident.asyncjobs.remove_ipset",
            {"name": self.name},
        )

    def on_action_refresh_source(self, value):
        """Fetch latest data from source, then sync to all instances."""
        if self.refresh_from_source():
            self.sync()

    def sync(self):
        """Broadcast this ipset to all instances."""
        if not self.is_enabled:
            return
        from mojo.apps import jobs
        jobs.broadcast_execute(
            "mojo.apps.incident.asyncjobs.sync_ipset",
            {"name": self.name, "cidrs": self.cidrs},
        )
        self.last_synced = dates.utcnow()
        self.sync_error = None
        self.save(update_fields=["last_synced", "sync_error"])

    def refresh_from_source(self):
        """Fetch latest CIDR data from the configured source."""
        if self.source == "manual":
            return False

        try:
            if self.source == "ipdeny":
                data = self._fetch_ipdeny()
            elif self.source == "abuseipdb":
                data = self._fetch_abuseipdb()
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
            return None
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
