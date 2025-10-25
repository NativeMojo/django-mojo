from django.db import models
from django.utils import timezone
from mojo.models import MojoModel
from mojo.helpers import dates


class PhoneNumber(models.Model, MojoModel):
    """
    Stores phone number lookup data with expiration for re-lookup.
    Caches carrier, line type, caller name, and validity information from Twilio/AWS lookups.
    Pure cache - not tied to users or organizations. Shared across entire system to minimize API charges.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    # Phone Number Fields
    phone_number = models.CharField(max_length=20, unique=True, db_index=True,
                                   help_text="E.164 formatted phone number (+1234567890)")
    country_code = models.CharField(max_length=5, db_index=True,
                                  help_text="Country code (e.g., US, CA, GB)")

    # Lookup Data - Carrier/Line Type
    carrier = models.CharField(max_length=100, blank=True, null=True,
                             help_text="Carrier/operator name")
    line_type = models.CharField(max_length=20, blank=True, null=True, db_index=True,
                               help_text="mobile, landline, voip, etc.")
    is_mobile = models.BooleanField(default=False, db_index=True)
    is_voip = models.BooleanField(default=False, db_index=True)
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
        VIEW_PERMS = ["view_phone_numbers", "manage_phone_numbers"]
        SAVE_PERMS = ["manage_phone_numbers"]
        DELETE_PERMS = ["manage_phone_numbers"]
        SEARCH_FIELDS = ["phone_number", "carrier", "registered_owner"]
        LIST_DEFAULT_FILTERS = {"is_valid": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "phone_number", "country_code", "carrier",
                          "line_type", "is_mobile", "is_voip", "is_valid",
                          "registered_owner", "caller_type"]
            },
            "default": {
                "fields": ["id", "phone_number", "country_code", "carrier",
                          "line_type", "is_mobile", "is_voip", "is_valid",
                          "registered_owner", "caller_type",
                          "lookup_expires_at", "last_lookup_at", "created"]
            },
            "full": {
                "exclude": []
            }
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

    def refresh(self):
        from mojo.apps.phonehub.services.twilio import lookup
        resp = lookup(self.phone_number)
        if resp.error:
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
        self.lookup_expires_at = dates.add(days=90)
        self.lookup_data = resp.lookup_data
        self.last_lookup_at = dates.utcnow()
        self.lookup_count += 1
        self.save()
        return True

    @classmethod
    def normalize(cls, phone_number):
        from mojo.apps import phonehub
        return phonehub.normalize(phone_number)

    @classmethod
    def lookup(cls, phone_number):
        normalized = cls.normalize(phone_number)
        phone, created = cls.objects.get_or_create(phone_number=normalized)
        if created or phone.needs_lookup:
            phone.refresh()
        return phone
