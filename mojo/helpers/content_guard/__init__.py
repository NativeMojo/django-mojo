"""
content_guard — deterministic content moderation for usernames and text.

Usage:
    from mojo.helpers import content_guard

    result = content_guard.check_username("some_user")
    result = content_guard.check_text("some comment")
"""
from .rules import load_rules, Rules
from .core import (
    check_username,
    check_text,
    suggest_username,
    on_rest_request,
    DEFAULT_POLICY,
)

__all__ = [
    "load_rules",
    "Rules",
    "check_username",
    "check_text",
    "suggest_username",
    "on_rest_request",
    "DEFAULT_POLICY",
]
