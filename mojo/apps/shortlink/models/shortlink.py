from django.db import models
from django.db.models import F
from mojo.models import MojoModel
from mojo.helpers import crypto, dates
from datetime import timedelta


class ShortLink(models.Model, MojoModel):
    """
    Shortened URL with optional OG metadata, file linking, and click tracking.
    """

    class RestMeta:
        CAN_SAVE = CAN_CREATE = True
        CAN_DELETE = True
        DEFAULT_SORT = "-created"
        VIEW_PERMS = ["manage_shortlinks", "owner"]
        SAVE_PERMS = ["manage_shortlinks", "owner"]
        SEARCH_FIELDS = ["code", "url", "source"]
        SEARCH_TERMS = ["code", "url", "source"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "code", "url", "source", "hit_count",
                    "expires_at", "is_active", "is_protected", "track_clicks",
                    "bot_passthrough", "metadata", "created", "modified"],
                "graphs": {
                    "user": "basic",
                    "group": "basic",
                }
            },
            "basic": {
                "fields": ["id", "code", "url", "source", "hit_count", "is_active"],
            },
            "list": {
                "fields": [
                    "id", "code", "url", "source", "hit_count",
                    "expires_at", "is_active", "created"],
                "graphs": {
                    "user": "basic",
                    "group": "basic",
                }
            },
        }

    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    code = models.CharField(max_length=10, unique=True, db_index=True)

    url = models.TextField(
        blank=True,
        default="",
        help_text="Destination URL (empty when using file-only link)"
    )

    source = models.CharField(
        max_length=50,
        blank=True,
        default="",
        db_index=True,
        help_text="Traceability tag: sms, email, fileman, etc."
    )

    user = models.ForeignKey(
        "account.User",
        related_name="shortlinks",
        null=True,
        blank=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text="User who created this short link"
    )

    group = models.ForeignKey(
        "account.Group",
        related_name="shortlinks",
        null=True,
        blank=True,
        default=None,
        on_delete=models.CASCADE,
        help_text="Group that owns this short link"
    )

    file = models.ForeignKey(
        "fileman.File",
        related_name="shortlinks",
        null=True,
        blank=True,
        default=None,
        on_delete=models.SET_NULL,
        help_text="Optional linked file for file-sharing shortlinks"
    )

    hit_count = models.IntegerField(default=0)

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="When this link expires (null = never)"
    )

    is_active = models.BooleanField(default=True)

    track_clicks = models.BooleanField(
        default=False,
        help_text="Log each visit with IP, user-agent, referer"
    )

    resolve_file = models.BooleanField(
        default=True,
        help_text="True = resolve file download URL dynamically per click; False = use stored url"
    )

    bot_passthrough = models.BooleanField(
        default=False,
        help_text="True = skip bot detection and OG preview, always redirect"
    )

    is_protected = models.BooleanField(
        default=False,
        help_text="Protected links are not deleted by the cleanup job"
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="OG/meta tags and scraped preview cache"
    )

    class Meta:
        indexes = [
            models.Index(fields=["source", "is_active"]),
            models.Index(fields=["expires_at", "is_active"]),
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["group", "is_active"]),
        ]

    def __str__(self):
        return f"{self.code} → {self.url[:60] if self.url else '(file)'}"

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return dates.utcnow() > self.expires_at

    @classmethod
    def create(cls, url="", source="", expire_days=3, expire_hours=0,
               metadata=None, track_clicks=False, resolve_file=True,
               bot_passthrough=False, is_protected=False,
               user=None, group=None, file=None):
        """Create a short link with a unique code."""
        code = cls._generate_code()

        expires_at = None
        total_hours = (expire_days * 24) + expire_hours
        if total_hours > 0:
            expires_at = dates.utcnow() + timedelta(hours=total_hours)

        return cls.objects.create(
            code=code,
            url=url,
            source=source,
            expires_at=expires_at,
            metadata=metadata or {},
            track_clicks=track_clicks,
            resolve_file=resolve_file,
            bot_passthrough=bot_passthrough,
            is_protected=is_protected,
            user=user,
            group=group,
            file=file,
        )

    @classmethod
    def _generate_code(cls, length=7, max_attempts=5):
        """Generate a unique short code."""
        for _ in range(max_attempts):
            code = crypto.random_string(length, True, True, False)
            if not cls.objects.filter(code=code).exists():
                return code
        raise RuntimeError(f"Failed to generate unique short code after {max_attempts} attempts")

    def resolve(self):
        """
        Resolve destination URL, increment hit_count, record metric.
        Returns the destination URL or None if expired/inactive.
        """
        if not self.is_active or self.is_expired:
            return None

        # Atomic hit_count increment
        ShortLink.objects.filter(pk=self.pk).update(hit_count=F("hit_count") + 1)

        # Record metric
        try:
            from mojo.apps import metrics
            metrics.record("shortlink:click", category="shortlinks", account="global")
            if self.source:
                metrics.record(f"shortlink:click:{self.source}", category="shortlinks", account="global")
        except Exception:
            pass  # metrics are best-effort

        # Resolve destination
        if self.file and self.resolve_file:
            return self.file.generate_download_url()

        return self.url or None

    def log_click(self, request):
        """Log a click if track_clicks is enabled."""
        if not self.track_clicks:
            return None
        from .click import ShortLinkClick
        return ShortLinkClick.objects.create(
            shortlink=self,
            ip=getattr(request, "ip", None),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
            referer=request.META.get("HTTP_REFERER", "")[:2000],
            is_bot=is_bot_user_agent(request.META.get("HTTP_USER_AGENT", "")),
        )

    def get_og_metadata(self):
        """
        Return merged OG metadata dict.
        Custom keys override scraped keys.
        """
        scraped = self.metadata.get("_scraped", {})
        # Custom keys are everything except _-prefixed keys
        custom = {k: v for k, v in self.metadata.items() if not k.startswith("_")}
        merged = dict(scraped)
        merged.update(custom)
        return merged


# Bot user-agent detection
BOT_SIGNATURES = [
    "Slackbot", "Twitterbot", "facebookexternalhit",
    "LinkedInBot", "Discordbot", "TelegramBot",
    "WhatsApp", "Applebot", "Googlebot",
    "com.google.android.apps.messaging",
    "Instagram",
]


def is_bot_user_agent(user_agent):
    """Check if a user-agent string belongs to a known bot/preview crawler."""
    if not user_agent:
        return False
    ua_lower = user_agent.lower()
    return any(sig.lower() in ua_lower for sig in BOT_SIGNATURES)
