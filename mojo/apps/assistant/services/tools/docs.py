"""Docs domain tools — fetch django-mojo framework documentation."""
import re

import requests

from mojo.helpers.settings import settings

DEFAULT_BASE_URL = "https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/"
DEFAULT_MAX_LENGTH = 20000
DEFAULT_TIMEOUT = 10
USER_AGENT = "Mojo-Assistant/1.0"

# Regex to find markdown links: [text](path) or | [text](path) |
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md[^)]*)\)")


def _fetch_doc(url):
    """Fetch a raw doc URL. Returns (content, error_dict)."""
    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except requests.exceptions.Timeout:
        return None, {"error": f"Request timed out after {DEFAULT_TIMEOUT}s"}
    except requests.exceptions.ConnectionError:
        return None, {"error": "Could not connect to documentation server"}
    except requests.exceptions.RequestException:
        return None, {"error": "Request failed"}

    if resp.status_code == 404:
        return None, {"error": f"Document not found: {url.split('/docs/')[-1] if '/docs/' in url else url}"}
    if resp.status_code == 403:
        return None, {"error": "GitHub rate limit reached. Try again in a few minutes."}
    if resp.status_code != 200:
        return None, {"error": f"HTTP {resp.status_code} fetching documentation"}

    return resp.text, None


def _find_topic_in_index(index_content, topic):
    """
    Search a README index for links matching a topic keyword.

    Returns list of (link_text, path) tuples sorted by relevance.
    """
    topic_lower = topic.lower()
    matches = []

    for line in index_content.split("\n"):
        line_lower = line.lower()
        if topic_lower not in line_lower:
            continue

        for link_text, link_path in _LINK_RE.findall(line):
            # Skip non-doc links (anchors, external URLs)
            if link_path.startswith("http") or link_path.startswith("#"):
                continue
            matches.append((link_text.strip(), link_path.strip()))

    return matches


def _normalize_path(path):
    """Normalize and validate a doc path. Returns (clean_path, error)."""
    path = path.strip().strip("/")

    # Strip leading "docs/" if present
    if path.startswith("docs/"):
        path = path[5:]

    # Security: reject path traversal
    if ".." in path:
        return None, {"error": "Path traversal ('..') is not allowed"}

    # Must end in .md
    if not path.endswith(".md"):
        path = path.rstrip("/") + "/README.md"

    return path, None


def _tool_read_docs(params, user):
    path = params.get("path", "").strip()
    topic = params.get("topic", "").strip()

    if not path and not topic:
        return {"error": "Provide either 'path' (e.g. 'django_developer/account/push.md') or 'topic' (e.g. 'push notifications')"}

    base_url = settings.get("LLM_DOCS_BASE_URL", DEFAULT_BASE_URL)
    base_url = base_url.rstrip("/") + "/"
    max_length = settings.get("LLM_BROWSE_MAX_LENGTH", DEFAULT_MAX_LENGTH, kind="int")

    if path:
        # Direct path fetch
        clean_path, err = _normalize_path(path)
        if err:
            return err

        content, err = _fetch_doc(f"{base_url}{clean_path}")
        if err:
            return err

        truncated = len(content) > max_length
        return {
            "path": clean_path,
            "content": content[:max_length],
            "content_length": len(content),
            "truncated": truncated,
        }

    # Topic-based lookup: search the indexes for matching links
    # Fetch django_developer README (most comprehensive index)
    dev_index, err = _fetch_doc(f"{base_url}django_developer/README.md")
    if err:
        return err

    matches = _find_topic_in_index(dev_index, topic)

    # Also check the web_developer README
    web_index, _ = _fetch_doc(f"{base_url}web_developer/README.md")
    if web_index:
        web_matches = _find_topic_in_index(web_index, topic)
        for text, link_path in web_matches:
            # Prefix with web_developer/ since paths are relative to that README
            full_path = f"web_developer/{link_path}"
            matches.append((text, full_path))

    if not matches:
        # No match — return the django_developer index so the LLM can browse
        truncated = len(dev_index) > max_length
        return {
            "path": "django_developer/README.md",
            "content": dev_index[:max_length],
            "content_length": len(dev_index),
            "truncated": truncated,
            "note": f"No docs matched topic '{topic}'. Returning the index — browse for the right section.",
        }

    # Fetch the first match
    best_text, best_path = matches[0]

    # Resolve relative path — matches from django_developer README are relative to it
    if not best_path.startswith("web_developer/"):
        best_path = f"django_developer/{best_path}"

    content, err = _fetch_doc(f"{base_url}{best_path}")
    if err:
        return err

    truncated = len(content) > max_length
    result = {
        "path": best_path,
        "content": content[:max_length],
        "content_length": len(content),
        "truncated": truncated,
    }

    # Note other matches if any
    if len(matches) > 1:
        others = [f"{text} ({p})" for text, p in matches[1:5]]
        result["other_matches"] = others

    return result


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "read_docs",
        "description": (
            "Fetch django-mojo framework documentation. Use 'path' for a specific doc "
            "(e.g. 'django_developer/account/push.md') or 'topic' for keyword search "
            "(e.g. 'push notifications', 'rate limiting', 'job queue'). "
            "Returns raw markdown content. Use this to look up how framework features work, "
            "check available settings, or find code examples."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative doc path (e.g. 'django_developer/account/push.md')",
                },
                "topic": {
                    "type": "string",
                    "description": "Free-text topic to search for (e.g. 'push notifications')",
                },
            },
        },
        "handler": _tool_read_docs,
        "permission": "view_admin",
    },
]
