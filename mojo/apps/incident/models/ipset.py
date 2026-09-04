from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates, logit
from mojo.apps.incident.services.firewall_truth import (
    FirewallTruthError,
    canonical_ipv4_networks,
    canonical_set_name,
    bounded_error,
)


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
        DELETE_PERMS = ["manage_security", "security"]
        SEARCH_FIELDS = ["name", "description"]
        SENSITIVE_FIELDS = ["source_key"]
        NO_SAVE_FIELDS = [
            "id", "pk", "created", "modified", "is_enabled", "cidr_count",
            "last_synced", "sync_error",
        ]
        CAN_DELETE = False
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

    def save(self, *args, **kwargs):
        """Central lifecycle guard for every model-owned write path."""
        from mojo.apps.incident.services import firewall_truth
        lease = getattr(self, "_desired_lease", None)
        owns_lease = lease is None
        try:
            if owns_lease:
                lease = firewall_truth.acquire_desired_state(180)
            self.name = canonical_set_name(self.name)
            aggregate_name = firewall_truth.permanent_set_name()
            if self.name == aggregate_name:
                raise FirewallTruthError(
                    "reserved_set_name",
                    "configured permanent firewall set name is reserved")
            allowed_reserved = bool(getattr(self, "_allow_reserved_name", False))
            if (self.name in self.THREAT_CACHE_SETS or
                    self.name.startswith("mojo_")) and not allowed_reserved:
                raise FirewallTruthError(
                    "reserved_set_name", "firewall set namespace is reserved")
            lifecycle = bool(getattr(self, "_lifecycle_write", False))
            # Any path saving data gets the same all-or-nothing canonical form.
            if self.data:
                self.set_data(self.cidrs)
            else:
                self.data = ""
                self.cidr_count = 0
            prior = None
            if self.pk is None:
                # Creation never mutates the kernel. Explicit enable is a separate,
                # governed operation with a checked outcome.
                self.is_enabled = False
            else:
                prior = type(self).objects.filter(pk=self.pk).values(
                    "name", "is_enabled", "data").first()
                if prior:
                    if prior["name"] != self.name:
                        raise FirewallTruthError(
                            "immutable_set_name", "firewall set names are immutable")
                    if prior["is_enabled"] != self.is_enabled and not lifecycle:
                        raise FirewallTruthError(
                            "lifecycle_required",
                            "firewall set state changes require enable/disable actions")
                    if prior["data"] != self.data and prior["is_enabled"]:
                        self.sync_error = "pending checked firewall reconciliation"
                        if kwargs.get("update_fields") is not None:
                            fields = set(kwargs["update_fields"])
                            fields.update(("data", "cidr_count", "sync_error", "modified"))
                            kwargs["update_fields"] = list(fields)
            desired_changed = (
                prior is None or prior["name"] != self.name or
                prior["is_enabled"] != self.is_enabled or
                prior["data"] != self.data)
            if desired_changed:
                firewall_truth.advance_fences(lease, [("set", self.name)])
            return super().save(*args, **kwargs)
        except FirewallTruthError as err:
            from mojo import errors as merrors
            raise merrors.ValueException(str(err)) from err
        finally:
            if owns_lease:
                firewall_truth.release_desired_state(lease)

    def delete(self, *args, **kwargs):
        from mojo import errors as merrors
        raise merrors.ValueException(
            "firewall sets are retired with disable; deletion is not supported")

    def on_rest_pre_save(self, changed_fields, created):
        if created:
            self.is_enabled = False
        if not created and "name" in changed_fields:
            from mojo import errors as merrors
            raise merrors.ValueException("firewall set names are immutable")

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
        try:
            canonical = canonical_ipv4_networks(cidr_list)
        except FirewallTruthError as err:
            from mojo import errors as merrors
            raise merrors.ValueException(str(err)) from err
        self.data = "\n".join(canonical)
        self.cidr_count = len(canonical)

    def on_action_sync(self, value):
        """Broadcast ipset to all instances."""
        return self.sync()

    def on_action_enable(self, value):
        if self.is_cache_only:
            from mojo import errors as merrors
            raise merrors.ValueException(
                f"'{self.name}' is a cache-only threat list for geoip "
                f"detection — enabling it would kernel-block every listed IP "
                f"fleet-wide and is not permitted")
        return self.enable()

    def on_action_disable(self, value):
        """Disable and remove ipset from all instances."""
        return self.disable()

    def on_action_refresh_source(self, value):
        """Fetch latest data from source, then sync to all instances."""
        if self.refresh_from_source():
            return self.sync()
        return {"status": "unknown", "ok": False,
                "error": {"code": "source_refresh_failed"}}

    def _save_lifecycle(self, fields):
        self._lifecycle_write = True
        self._allow_reserved_name = self.name in self.THREAT_CACHE_SETS
        try:
            self.save(update_fields=fields)
        finally:
            self._lifecycle_write = False

    def enable(self):
        if self.is_cache_only:
            from mojo import errors as merrors
            raise merrors.ValueException(
                f"'{self.name}' is a cache-only threat list for geoip "
                "detection and cannot be enabled")
        return self._change_and_sync(True)

    def disable(self):
        return self._change_and_sync(False)

    def _change_and_sync(self, enabled):
        from mojo.apps.incident.services import firewall_truth
        lease = None
        try:
            lease = firewall_truth.acquire_desired_state(180)
            self._desired_lease = lease
            current = type(self).objects.filter(pk=self.pk).values(
                "modified").first()
            if current is None or current["modified"] != self.modified:
                return {
                    "status": "partial", "ok": False,
                    "error": {
                        "code": "generation_superseded",
                        "message": "newer IPSet state won before lifecycle write",
                    },
                }
            self.set_enabled_desired(enabled)
            return self._sync_locked(lease)
        except firewall_truth.FirewallTruthError as err:
            return {"status": "unknown", "ok": False,
                    "error": {"code": err.code, "message": str(err)}}
        finally:
            self._desired_lease = None
            firewall_truth.release_desired_state(lease)

    def set_enabled_desired(self, enabled):
        if enabled and self.is_cache_only:
            from mojo import errors as merrors
            raise merrors.ValueException(
                f"'{self.name}' is a cache-only threat list and cannot be enabled")
        if enabled and len(self.name) + 4 > 31:
            from mojo import errors as merrors
            raise merrors.ValueException(
                "firewall set name is too long for atomic replacement")
        self.is_enabled = bool(enabled)
        self.sync_error = ("pending checked firewall reconciliation" if enabled
                           else "pending checked firewall removal")
        self._save_lifecycle(["is_enabled", "sync_error", "modified"])

    def sync(self):
        """Dispatch desired state and persist only checked host truth."""
        from mojo.apps.incident.services import firewall_truth
        lease = None
        try:
            lease = firewall_truth.acquire_desired_state(180)
            return self._sync_locked(lease)
        except firewall_truth.FirewallTruthError as err:
            return {"status": "unknown", "ok": False,
                    "error": {"code": err.code, "message": str(err)}}
        finally:
            firewall_truth.release_desired_state(lease)

    def _sync_locked(self, lease):
        """Reconcile while the global desired-state generation is stable."""
        # Hard circuit breaker: the cache-only threat lists must never reach
        # the kernel firewall, even if is_enabled was force-set via a generic
        # field save (the enable action also rejects them with a 400).
        if self.is_cache_only:
            return {"status": "unknown", "ok": False,
                    "error": {"code": "cache_only_set"}}
        from mojo.apps.incident.services import firewall_truth
        dispatched_at = dates.utcnow()
        pending = "pending checked firewall reconciliation"
        # Refuse stale model instances before dispatch. The direct update is a
        # revision claim, not a lifecycle bypass: it changes no desired field.
        claimed = type(self).objects.filter(
            pk=self.pk, modified=self.modified,
            is_enabled=self.is_enabled).update(
                last_synced=dispatched_at, sync_error=pending,
                modified=dispatched_at)
        if not claimed:
            return {
                "status": "partial", "ok": False,
                "error": {
                    "code": "generation_superseded",
                    "message": "newer IPSet desired state won before dispatch",
                },
            }
        self.last_synced = dispatched_at
        self.sync_error = pending
        self.modified = dispatched_at
        generation = dispatched_at
        result = firewall_truth.reconcile_set(
            self.name, self.cidrs, present=self.is_enabled, lease=lease)
        if result.get("status") == "verified" and result.get("ok") is True:
            self.sync_error = None
        else:
            code, message = bounded_error(result)
            self.sync_error = f"{code}: {message}"[:512]
        # Modified may have moved while the network wait was in flight. Never
        # overwrite newer desired state with this older receipt.
        updated = type(self).objects.filter(
            pk=self.pk, is_enabled=self.is_enabled,
            last_synced=self.last_synced, modified=generation).update(
                sync_error=self.sync_error)
        if not updated:
            result = dict(result)
            result.update(
                status="partial", ok=False,
                error={"code": "generation_superseded",
                       "message": "newer IPSet desired state superseded this receipt"})
        return result

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

            if data is not None:
                self.set_data(data)
                self.sync_error = None
                self.save(update_fields=[
                    "data", "cidr_count", "sync_error", "modified"])
                return True
            return False
        except Exception:
            self.sync_error = "source_refresh_failed"
            self.save(update_fields=["sync_error", "modified"])
            logit.exception("IPSet refresh failed for %s", self.name)
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
            row = cls.objects.filter(name=name).first()
            if row is None:
                row = cls(name=name, **{
                    **defaults,
                    "is_enabled": False,
                    "description": (
                        "Cache-only list for geoip detection — do NOT enable; "
                        "enabling would kernel-block every listed IP fleet-wide."
                    ),
                })
                row._allow_reserved_name = True
                row.save()
            else:
                row._allow_reserved_name = True
            if row.is_enabled:
                row.is_enabled = False
                row._lifecycle_write = True
                row.save(update_fields=["is_enabled", "modified"])
            rows.append(row)
        return rows

    @classmethod
    def create_country(cls, country_code, enabled=True):
        """Helper to create a country IPSet."""
        import re
        code = str(country_code).lower()
        if not re.fullmatch(r"[a-z]{2}", code):
            from mojo import errors as merrors
            raise merrors.ValueException("country_code must be two letters")
        row, unused = cls.objects.update_or_create(
            name=f"country_{code}", defaults={
                "kind": "country",
                "description": f"Block country: {code.upper()}",
                "source": "ipdeny",
                "source_url": f"https://www.ipdeny.com/ipblocks/data/countries/{code}.zone",
            })
        if bool(enabled) != row.is_enabled:
            row.enable() if enabled else row.disable()
        return row

    @classmethod
    def create_abuse_list(cls, api_key, enabled=True):
        """Helper to create an AbuseIPDB IPSet."""
        row, unused = cls.objects.update_or_create(
            name="abuse_ips",
            defaults={
                "kind": "abuse",
                "description": "AbuseIPDB blacklist (confidence 100%)",
                "source": "abuseipdb",
                "source_key": api_key,
            }
        )
        if bool(enabled) != row.is_enabled:
            row.enable() if enabled else row.disable()
        return row
