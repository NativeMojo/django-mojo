"""
ShortLink — URL shortener with OG previews, file linking, and click tracking.

Usage:
    from mojo.apps.shortlink import shorten

    # Basic SMS shortlink (expires in 3 days)
    url = shorten("https://example.com/verify?token=abc", source="sms")

    # With custom OG metadata for rich previews
    url = shorten(
        "https://example.com/invoice/123",
        source="email",
        metadata={"og:title": "Invoice #123", "og:description": "View your invoice"},
    )

    # File shortlink (resolves download URL dynamically)
    url = shorten(file=file_obj, source="fileman")

    # Transactional link — skip bot detection, just redirect
    url = shorten("https://example.com/reset?t=xyz", source="sms", bot_passthrough=True)

    # With click tracking
    url = shorten("https://example.com/promo", source="email", track_clicks=True)

    # Never expires
    url = shorten("https://example.com/docs", expire_days=0, expire_hours=0)
"""


def shorten(url="", file=None, source="", expire_days=3, expire_hours=0,
            metadata=None, track_clicks=False, resolve_file=True,
            bot_passthrough=False, is_protected=False,
            user=None, group=None, base_url=None):
    """
    Create a shortened URL and return the full short URL string.

    Args:
        url: Destination URL (required unless file is provided).
        file: fileman.File instance for file-sharing shortlinks.
        source: Traceability tag ("sms", "email", "fileman", etc.).
        expire_days: Days until expiry (default 3). 0 + expire_hours=0 = never.
        expire_hours: Additional hours until expiry (default 0).
        metadata: Dict of OG/meta tags, e.g. {"og:title": "My Page"}.
        track_clicks: Log each visit with IP, user-agent, referer.
        resolve_file: When file is set — True=dynamic URL per click, False=snapshot.
        bot_passthrough: True = skip bot detection/OG preview, always redirect.
        user: User who created the link.
        group: Group scope.
        base_url: Override base URL (default: SHORTLINK_BASE_URL or BASE_URL).

    Returns:
        Full short URL string, e.g. "https://itf.io/s/Xk9mR2p"
    """
    from .models import ShortLink
    from mojo.helpers.settings import settings

    if not url and not file:
        raise ValueError("Either url or file must be provided")

    # If file provided without a url and resolve_file is False, snapshot the URL now
    if file and not url and not resolve_file:
        url = file.generate_download_url() or ""

    link = ShortLink.create(
        url=url,
        source=source,
        expire_days=expire_days,
        expire_hours=expire_hours,
        metadata=metadata,
        track_clicks=track_clicks,
        resolve_file=resolve_file,
        bot_passthrough=bot_passthrough,
        is_protected=is_protected,
        user=user,
        group=group,
        file=file,
    )

    # Record creation metric
    try:
        from mojo.apps import metrics
        metrics.record("shortlink:created", category="shortlinks", account="global")
    except Exception:
        pass

    # Fire async scrape job if no custom OG data and not bot_passthrough
    if not bot_passthrough and not any(k.startswith("og:") for k in (metadata or {})):
        target = url or (file.generate_download_url() if file else "")
        if target and target.startswith("http"):
            try:
                from mojo.apps import jobs
                jobs.publish(
                    "mojo.apps.shortlink.services.scraper.scrape_og_metadata",
                    payload={"shortlink_id": link.pk},
                    max_retries=2,
                    max_exec_seconds=15,
                )
            except Exception:
                pass  # scraping is best-effort

    if not base_url:
        base_url = settings.get("SHORTLINK_BASE_URL", None) or \
                   settings.get("BASE_URL", "")

    return f"{base_url.rstrip('/')}/s/{link.code}"
