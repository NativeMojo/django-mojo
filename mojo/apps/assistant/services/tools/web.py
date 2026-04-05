"""Web domain tools — fetch and extract content from web pages."""
import ipaddress
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from mojo.helpers.settings import settings

# Tags that are boilerplate on almost every page
STRIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "iframe"}

DEFAULT_MAX_LENGTH = 20000
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mojo-Assistant/1.0"
MAX_REDIRECTS = 3

# Private/reserved IP ranges that should never be fetched (SSRF protection)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(hostname):
    """Resolve hostname and check if it points to a private/reserved IP."""
    try:
        results = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in results:
            ip = ipaddress.ip_address(sockaddr[0])
            for network in _BLOCKED_NETWORKS:
                if ip in network:
                    return True
    except socket.gaierror:
        pass
    return False


def _extract_text(html, selector=None):
    """Parse HTML with BeautifulSoup, optionally narrow by CSS selector, return clean text."""
    soup = BeautifulSoup(html, "html.parser")

    # Narrow to selector if provided
    if selector:
        target = soup.select_one(selector)
        if not target:
            return None, soup.title.string if soup.title else None
        soup = target

    # Strip boilerplate tags
    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()

    title = None
    if hasattr(soup, "title") and soup.title:
        title = soup.title.string

    text = soup.get_text(separator="\n", strip=True)
    return text, title


def _tool_browse_url(params, user):
    url = params.get("url", "").strip()
    selector = params.get("selector")

    if not url:
        return {"error": "url is required"}

    # Validate scheme
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": f"Unsupported scheme '{parsed.scheme}'. Only http and https are allowed."}

    if not parsed.hostname:
        return {"error": "Invalid URL — no hostname found"}

    # SSRF protection: resolve hostname and reject private IPs
    if _is_private_ip(parsed.hostname):
        return {"error": "Cannot fetch private or internal addresses"}

    max_length = settings.get("LLM_BROWSE_MAX_LENGTH", DEFAULT_MAX_LENGTH, kind="int")
    timeout = settings.get("LLM_BROWSE_TIMEOUT", DEFAULT_TIMEOUT, kind="int")

    try:
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        resp = session.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.exceptions.TooManyRedirects:
        return {"error": f"Too many redirects (max {MAX_REDIRECTS})"}
    except requests.exceptions.ConnectionError:
        return {"error": f"Could not connect to {parsed.hostname}"}
    except requests.exceptions.Timeout:
        return {"error": f"Request timed out after {timeout}s"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {str(e)[:200]}"}

    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code} from {parsed.hostname}"}

    content_type = resp.headers.get("content-type", "")

    # Non-HTML content: return raw text
    if "html" not in content_type.lower():
        content = resp.text[:max_length]
        return {
            "url": url,
            "title": None,
            "content": content,
            "content_length": len(resp.text),
            "truncated": len(resp.text) > max_length,
        }

    # HTML content: parse with BeautifulSoup
    text, title = _extract_text(resp.text, selector=selector)

    if text is None and selector:
        return {"error": f"CSS selector '{selector}' matched nothing on the page"}

    truncated = len(text) > max_length
    content = text[:max_length]

    return {
        "url": url,
        "title": title,
        "content": content,
        "content_length": len(text),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "browse_url",
        "description": (
            "Fetch a web page and return its content as clean, readable text. "
            "Use this to read documentation, reference pages, changelogs, or any public URL. "
            "Optionally pass a CSS selector to extract a specific section of the page. "
            "Only http/https URLs are allowed. Content is truncated to ~20K chars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (http or https only)",
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to narrow content (e.g. 'main', '#content', '.docs-body')",
                },
            },
            "required": ["url"],
        },
        "handler": _tool_browse_url,
        "permission": "view_admin",
    },
]
