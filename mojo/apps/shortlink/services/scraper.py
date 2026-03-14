"""
Async job: scrape OG metadata from a shortlink's destination URL.

Fired on shortlink creation when no custom OG data is provided
and bot_passthrough is False. Stores scraped tags in metadata["_scraped"].
"""
import re
import ipaddress
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError
from mojo.helpers import dates, logit


SCRAPE_TIMEOUT = 5  # seconds
SCRAPE_MAX_BYTES = 256 * 1024  # 256 KB — we only need the <head>
SCRAPE_USER_AGENT = "MojoLinkPreview/1.0"


class _OGParser(HTMLParser):
    """Minimal HTML parser that extracts <meta property="og:..." content="..."> tags."""

    def __init__(self):
        super().__init__()
        self.og_tags = {}
        self._in_head = False
        self._past_head = False

    def handle_starttag(self, tag, attrs):
        if self._past_head:
            return
        if tag == "head":
            self._in_head = True
            return
        if tag == "meta" and self._in_head:
            attrs_dict = dict(attrs)
            prop = attrs_dict.get("property", "") or attrs_dict.get("name", "")
            content = attrs_dict.get("content", "")
            if prop and content and (prop.startswith("og:") or prop.startswith("twitter:")):
                self.og_tags[prop] = content

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
            self._past_head = True


def _is_private_url(url):
    """Reject private/internal URLs to prevent SSRF."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return True
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except (ValueError, TypeError):
        # hostname is a domain name, not an IP — allow it
        return False


def _fetch_og_tags(url):
    """Fetch and parse OG tags from a URL. Returns dict or empty dict."""
    if not url or not url.startswith("http"):
        return {}
    if _is_private_url(url):
        return {}

    try:
        req = Request(url, headers={"User-Agent": SCRAPE_USER_AGENT})
        with urlopen(req, timeout=SCRAPE_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return {}
            raw = resp.read(SCRAPE_MAX_BYTES)
            html = raw.decode("utf-8", errors="replace")

        parser = _OGParser()
        parser.feed(html)
        return parser.og_tags

    except Exception as e:
        logit.debug(f"shortlink scraper: failed to fetch {url}: {e}")
        return {}


def scrape_og_metadata(job):
    """
    Job handler: scrape OG metadata for a shortlink.

    payload: {"shortlink_id": <int>}
    """
    from mojo.apps.shortlink.models import ShortLink

    shortlink_id = job.payload.get("shortlink_id")
    if not shortlink_id:
        return "failed:no_shortlink_id"

    try:
        link = ShortLink.objects.get(pk=shortlink_id)
    except ShortLink.DoesNotExist:
        return "failed:not_found"

    # Skip if custom OG data already provided
    if any(k.startswith("og:") for k in link.metadata if not k.startswith("_")):
        return "skipped:custom_og_present"

    # Skip if bot_passthrough
    if link.bot_passthrough:
        return "skipped:bot_passthrough"

    target_url = link.url
    if not target_url and link.file:
        try:
            target_url = link.file.generate_download_url()
        except Exception:
            return "skipped:no_url"

    if not target_url:
        return "skipped:no_url"

    og_tags = _fetch_og_tags(target_url)
    if not og_tags:
        return "completed:no_tags_found"

    og_tags["scraped_at"] = dates.utcnow().isoformat()
    link.metadata["_scraped"] = og_tags
    link.save(update_fields=["metadata", "modified"])

    return "completed"
