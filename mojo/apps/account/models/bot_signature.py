from django.db import models
from mojo.models import MojoModel
from mojo.helpers import dates

SIG_TYPE_CHOICES = [
    ('ip', 'IP Address'),
    ('subnet_24', 'Subnet /24'),
    ('subnet_16', 'Subnet /16'),
    ('user_agent', 'User Agent'),
    ('fingerprint', 'Browser Fingerprint'),
    ('signal_set', 'Signal Set (Campaign)'),
]


class BotSignature(models.Model, MojoModel):
    """
    Adaptive bot signature registry.

    Auto-populated by the BotLearner background job after confirmed high-confidence
    blocks. Manually manageable via the operator portal (full CRUD via RestMeta).

    Auto-registered entries always have an expires_at TTL.
    Manual entries (source='manual') may have expires_at=None (permanent).

    Pre-screen checks a Redis cache of active signatures before running full scoring,
    so matched entries are caught at the gate with no scoring overhead.
    """
    sig_type = models.CharField(max_length=16, choices=SIG_TYPE_CHOICES, db_index=True)
    value = models.CharField(max_length=512, db_index=True)
    source = models.CharField(max_length=8, default='auto')  # auto | manual
    confidence = models.IntegerField(default=0)
    hit_count = models.IntegerField(default=0)
    block_count = models.IntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True, default='')

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ['manage_users', 'admin_security']
        SAVE_PERMS = ['manage_users', 'admin_security']
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'sig_type', 'value', 'source', 'confidence',
                    'hit_count', 'block_count', 'expires_at', 'is_active',
                    'created', 'modified', 'notes',
                ]
            },
            'list': {
                'fields': [
                    'id', 'sig_type', 'value', 'source', 'confidence',
                    'hit_count', 'is_active', 'expires_at', 'modified',
                ]
            },
        }

    class Meta:
        unique_together = ('sig_type', 'value')
        ordering = ['-modified']

    def __str__(self):
        return f"BotSignature<{self.sig_type}:{self.value[:40]}>"

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return dates.utcnow() > self.expires_at
