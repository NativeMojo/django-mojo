import re

from django.db import models
from django.utils import timezone
from mojo.models import MojoModel
from mojo.helpers import dates


# The cache is deliberately asymmetric: a successful carrier verdict is cached
# for a quarter of a year, while a provider error is cached only long enough to
# stop a retry storm and a re-bill (backing off to a 24h ceiling).
LOOKUP_TTL_DAYS = 90
LOOKUP_ERROR_TTL_MINUTES = 15
LOOKUP_ERROR_TTL_MAX_MINUTES = 1440
LOOKUP_ERROR_MAX_CHARS = 200

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class PhoneNumber(models.Model, MojoModel):
    """
    Stores phone number lookup data with expiration for re-lookup.
    Caches carrier, line type, caller name, and validity information from Twilio/AWS lookups.
    Pure cache - not tied to users or organizations. Shared across entire system to minimize API charges.

    Two TTLs share one row. A successful lookup writes the carrier fields and
    caches them for LOOKUP_TTL_DAYS. A provider error writes nothing but the
    error marker in `lookup_data` and a short, exponentially backing-off
    LOOKUP_ERROR_TTL_MINUTES expiry, so the failure is cached (no re-bill) and
    the previous verdict is never overwritten by someone else's downtime. Read
    `lookup_unavailable` before reading `is_valid`.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    # Phone Number Fields
    phone_number = models.CharField(max_length=20, unique=True, db_index=True,
                                   help_text="E.164 formatted phone number (+1234567890)")

    country_code = models.CharField(max_length=5, db_index=True, null=True, blank=True)
    region = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=3, db_index=True, null=True, blank=True)

    # Lookup Data - Carrier/Line Type
    carrier = models.CharField(max_length=100, blank=True, null=True,
                             help_text="Carrier/operator name")
    line_type = models.CharField(max_length=20, blank=True, null=True, db_index=True,
                               help_text="mobile, landline, voip, etc.")
    is_mobile = models.BooleanField(default=False, db_index=True)
    is_voip = models.BooleanField(default=False, db_index=True)
    # is_valid is the carrier verdict from the last SUCCESSFUL lookup. The
    # error path never writes it, so it is meaningless while
    # `lookup_unavailable` is true — check that property first.
    is_valid = models.BooleanField(default=True, db_index=True,
                                 help_text="Whether phone number is valid/reachable")

    # Caller Identity Data (from Twilio Caller Name lookup)
    registered_owner = models.CharField(max_length=200, blank=True, null=True, db_index=True,
                                  help_text="Registered owner/caller name from carrier")
    owner_type = models.CharField(max_length=50, blank=True, null=True,
                                  help_text="BUSINESS or CONSUMER")

    # Address information (if available from caller name lookup)
    address_line1 = models.CharField(max_length=200, blank=True, null=True)
    address_city = models.CharField(max_length=100, blank=True, null=True)
    address_state = models.CharField(max_length=50, blank=True, null=True)
    address_zip = models.CharField(max_length=20, blank=True, null=True)
    address_country = models.CharField(max_length=5, blank=True, null=True)

    # Metadata
    lookup_provider = models.CharField(max_length=20, blank=True, null=True,
                                     help_text="twilio or aws")
    lookup_data = models.JSONField(default=dict, blank=True,
                                 help_text="Raw lookup response data")

    # Expiration
    lookup_expires_at = models.DateTimeField(db_index=True,
                                           help_text="When to re-lookup this number")
    lookup_count = models.IntegerField(default=0,
                                     help_text="Number of times this number has been looked up")
    last_lookup_at = models.DateTimeField(null=True, blank=True,
                                        help_text="Last successful lookup timestamp")

    class Meta:
        ordering = ['-created']
        indexes = [
            models.Index(fields=['phone_number']),
            models.Index(fields=['lookup_expires_at', 'is_valid']),
            models.Index(fields=['registered_owner']),
        ]

    class RestMeta:
        VIEW_PERMS = ["view_phone_numbers", "manage_phone_numbers", "comms", "manage_users"]
        SAVE_PERMS = ["manage_phone_numbers", "comms", "manage_users"]
        DELETE_PERMS = ["manage_phone_numbers"]
        SEARCH_FIELDS = ["phone_number", "carrier", "registered_owner"]
        # lookup_data only ever holds raw provider error text (the success path
        # clears it), so it is never served. Clients read lookup_unavailable.
        NO_SHOW_FIELDS = ["lookup_data"]
        GRAPHS = {
            "basic": {

            },
            "default": {
                "extra": ["lookup_unavailable"],
            },
        }

    def __str__(self):
        if self.registered_owner:
            return f"{self.phone_number} ({self.registered_owner})"
        return f"{self.phone_number} ({self.carrier or 'unknown'})"

    @property
    def needs_lookup(self):
        """Check if phone number lookup has expired."""
        if not self.lookup_expires_at:
            return True
        return timezone.now() >= self.lookup_expires_at

    @property
    def is_expired(self):
        """Alias for needs_lookup."""
        return self.needs_lookup

    @property
    def lookup_error(self):
        """Provider error text from the last failed lookup, or None."""
        return (self.lookup_data or {}).get("error")

    @property
    def lookup_unavailable(self):
        """
        True when the provider failed and this row carries no successful
        lookup younger than LOOKUP_TTL_DAYS.

        `is_valid` then carries no current information: a consumer must treat
        this as "no verdict", never as a verdict. A previously-good row that
        errors inside its TTL keeps serving its cached verdict, so a short
        outage changes nothing for it. The ceiling matters because a
        disconnected or reassigned number errors forever — a verdict the TTL
        already calls expired, that cannot be refreshed, is no verdict.
        """
        if not self.lookup_error:
            return False
        if self.last_lookup_at is None:
            return True
        return self.last_lookup_at < dates.subtract(days=LOOKUP_TTL_DAYS)

    @property
    def area_code(self):
        return self.area_code_info.get("area_code", "")

    _area_code_info = None
    @property
    def area_code_info(self):
        if self._area_code_info:
            return self._area_code_info
        from mojo.apps.phonehub import get_area_code_info
        self._area_code_info = get_area_code_info(self.phone_number)
        return self._area_code_info

    def refresh(self, *, lookup_fn=None):
        """
        Re-run the provider lookup for this number.

        `lookup_fn` is a test seam for the provider call. Production never
        passes it; when it is None the Twilio lookup service is used.
        """
        if lookup_fn is None:
            from mojo.apps.phonehub.services.twilio import lookup
            lookup_fn = lookup
        if not self.region and self.area_code_info and self.area_code_info.location:
            self.region = self.area_code_info.location.get("region", "")
            self.state = self.area_code_info.location.get("state", "")
            self.country_code = self.area_code_info.location.get("country", "")

        resp = lookup_fn(self.phone_number)
        if resp.error:
            self._record_lookup_error(resp.error)
            return False

        self.carrier = resp.carrier
        self.country_code = resp.country_code
        self.line_type = resp.line_type
        self.is_mobile = resp.is_mobile
        self.is_voip = resp.is_voip
        self.is_valid = resp.is_valid
        self.registered_owner = resp.caller_name
        self.owner_type = resp.caller_type
        self.lookup_provider = resp.lookup_provider
        self.lookup_expires_at = dates.add(days=LOOKUP_TTL_DAYS)
        # Success is the reset for the error marker.
        self.lookup_data = {}
        self.last_lookup_at = dates.utcnow()
        self.lookup_count += 1
        self.save()
        return True

    def _record_lookup_error(self, error):
        """
        Negative-cache a provider error.

        Writes only the error marker and a short, backing-off expiry. The
        carrier verdict fields (is_valid, carrier, line_type, is_mobile,
        is_voip, registered_owner), last_lookup_at and lookup_count are left
        alone: an outage is not a verdict, and it is not a successful lookup.
        The expiry is stamped unconditionally so a lapsed error row can never
        fall back into the NULL-expiry crash or a permanent re-bill loop.
        """
        data = dict(self.lookup_data or {})
        text = _ANSI_ESCAPE.sub("", str(error))[:LOOKUP_ERROR_MAX_CHARS]
        error_count = (data.get("error_count") or 0) + 1
        data["error"] = text
        data["error_at"] = dates.utcnow().isoformat()
        data["error_count"] = error_count
        self.lookup_data = data
        # The exponent is bounded before it is used: 2**7 * 15 already exceeds
        # the ceiling, and a long-lived error row must not build a huge int.
        backoff = LOOKUP_ERROR_TTL_MINUTES * (2 ** min(error_count - 1, 8))
        self.lookup_expires_at = dates.add(
            minutes=min(backoff, LOOKUP_ERROR_TTL_MAX_MINUTES))
        self.save()

    @classmethod
    def normalize(cls, phone_number):
        from mojo.apps import phonehub
        return phonehub.normalize(phone_number)

    @classmethod
    def lookup(cls, phone_number):
        normalized = cls.normalize(phone_number)
        if not normalized:
            # No cache key — inserting a row here moves the same NOT NULL
            # failure onto phone_number.
            return None
        phone = cls.objects.filter(phone_number=normalized).first()
        if phone is None:
            phone = cls(phone_number=normalized)
            phone.refresh()
        elif phone.is_expired:
            phone.refresh()
        return phone
