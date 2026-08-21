"""WebApp setup and day-2 operations for the Admin Assistant.

Every tool wraps the same service the matching Admin endpoint calls — same
authority, same fresh-auth window, same bounded projection — so chat is never
an easier path to an operation than the portal. Three things are excluded by
design and are handoffs, not tools: minting or rotating a deploy key (shown
exactly once, so it can never enter model context), buying a domain (a one-use
money-moving confirmation), and uploading a build from a laptop.
"""
from . import reads  # noqa: F401
