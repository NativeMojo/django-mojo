from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates

RISK_TIERS = ('unknown', 'low', 'medium', 'high', 'blocked')
RISK_TIER_CHOICES = [(t, t.capitalize()) for t in RISK_TIERS]


class BouncerDevice(models.Model, MojoModel):
    """
    Pre-auth device reputation tracking.

    Primary identity is `muid` — a server-generated UUID stored in an HttpOnly
    cookie (_muid) that the client cannot forge or rotate. `duid` is the client-
    claimed device ID from localStorage, kept for cross-referencing but not
    trusted on its own.

    Separate from UserDevice (which requires a logged-in user). After login,
    UserDevice stores the same `muid` to bridge pre-auth and post-auth tracking.
    """
    muid = models.CharField(max_length=64, unique=True, db_index=True)
    duid = models.CharField(max_length=255, db_index=True, default='', blank=True)
    msid = models.CharField(max_length=64, db_index=True, default='', blank=True)
    fingerprint_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    risk_tier = models.CharField(
        max_length=16, default='unknown', choices=RISK_TIER_CHOICES, db_index=True
    )
    event_count = models.IntegerField(default=0)
    block_count = models.IntegerField(default=0)
    last_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    linked_muids = models.JSONField(default=list, blank=True)
    first_seen = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True, db_index=True)

    created = models.DateTimeField(auto_now_add=True, editable=False)
    modified = models.DateTimeField(auto_now=True)

    class RestMeta:
        VIEW_PERMS = ['manage_users', 'view_security', 'manage_security', 'security', 'users']
        SAVE_PERMS = ['manage_users', 'manage_security', 'security', 'users']
        SEARCH_FIELDS = ['muid', 'duid', 'fingerprint_id', 'last_seen_ip']
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'muid', 'duid', 'msid', 'fingerprint_id', 'risk_tier',
                    'event_count', 'block_count', 'last_seen_ip', 'linked_muids',
                    'first_seen', 'last_seen',
                ]
            },
            'list': {
                'fields': [
                    'id', 'muid', 'duid', 'risk_tier', 'event_count',
                    'block_count', 'last_seen_ip', 'last_seen',
                ]
            },
        }

    class Meta:
        ordering = ['-last_seen']

    def __str__(self):
        return f"BouncerDevice<{self.muid} tier={self.risk_tier}>"

    def link_muid(self, other_muid):
        """Link another muid to this device (cross-session fingerprint stitching)."""
        other_str = str(other_muid)
        if other_str not in self.linked_muids and other_str != str(self.muid):
            self.linked_muids.append(other_str)
            self.save(update_fields=['linked_muids'])

    @classmethod
    def get_or_create_for_muid(cls, muid, duid='', ip=None):
        device, created = cls.objects.get_or_create(
            muid=muid,
            defaults={'duid': duid, 'last_seen_ip': ip or ''},
        )
        updates = {}
        if duid and device.duid != duid:
            updates['duid'] = duid
        if ip and device.last_seen_ip != ip:
            updates['last_seen_ip'] = ip
        if updates:
            for k, v in updates.items():
                setattr(device, k, v)
            device.save(update_fields=list(updates.keys()))
        return device, created
