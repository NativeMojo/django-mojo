from django.db import models
from mojo.models import MojoModel

STAGE_CHOICES = [
    ('assess', 'Assess'),
    ('submit', 'Submit'),
    ('event', 'Event'),
]

DECISION_CHOICES = [
    ('allow', 'Allow'),
    ('monitor', 'Monitor'),
    ('block', 'Block'),
    ('log', 'Log'),
]


class BouncerSignal(models.Model, MojoModel):
    """
    Audit log of every bouncer assessment event.
    One row per assess/submit/event API call. Read-only via REST.

    Tracks all four identity layers:
      muid — server-controlled device identity (HttpOnly cookie _muid)
      duid — client-claimed device identity (localStorage)
      msid — browser session identity (HttpOnly session cookie _msid)
      mtab — tab session identity (sessionStorage, JS-generated)
    """
    device = models.ForeignKey(
        'account.BouncerDevice', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='signals'
    )
    muid = models.CharField(max_length=64, db_index=True, default='', blank=True)
    duid = models.CharField(max_length=255, db_index=True, default='', blank=True)
    msid = models.CharField(max_length=64, db_index=True, default='', blank=True)
    mtab = models.CharField(max_length=64, db_index=True, default='', blank=True)
    session_id = models.CharField(max_length=64, db_index=True, default='', blank=True)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES, default='assess', db_index=True)
    ip_address = models.GenericIPAddressField(db_index=True)
    page_type = models.CharField(max_length=32, db_index=True, default='login')
    raw_signals = models.JSONField(default=dict, blank=True)
    server_signals = models.JSONField(default=dict, blank=True)
    risk_score = models.IntegerField(default=0, db_index=True)
    decision = models.CharField(
        max_length=16, choices=DECISION_CHOICES, default='allow', db_index=True
    )
    triggered_signals = models.JSONField(default=list, blank=True)
    token_nonce = models.CharField(max_length=64, null=True, blank=True)
    geo_ip = models.ForeignKey(
        'account.GeoLocatedIP', on_delete=models.SET_NULL, null=True, blank=True
    )
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ['manage_users', 'view_security', 'manage_security', 'security', 'users']
        SAVE_PERMS = []  # read-only via REST
        SEARCH_FIELDS = ['muid', 'duid', 'ip_address', 'decision']
        GRAPHS = {
            'default': {
                'fields': [
                    'id', 'muid', 'duid', 'msid', 'mtab', 'stage',
                    'ip_address', 'page_type', 'risk_score', 'decision',
                    'triggered_signals', 'created',
                ],
                'graphs': {'device': 'list'},
            },
            'list': {
                'fields': [
                    'id', 'muid', 'msid', 'stage', 'ip_address', 'page_type',
                    'risk_score', 'decision', 'created',
                ]
            },
            'detail': {
                'fields': [
                    'id', 'muid', 'duid', 'msid', 'mtab', 'session_id',
                    'stage', 'ip_address', 'page_type', 'risk_score', 'decision',
                    'triggered_signals', 'raw_signals', 'server_signals',
                    'token_nonce', 'created',
                ],
                'graphs': {
                    'device': 'default',
                    'geo_ip': 'default',
                },
            },
        }

    class Meta:
        ordering = ['-created']

    def __str__(self):
        return f"BouncerSignal<{self.muid} {self.decision} score={self.risk_score}>"
