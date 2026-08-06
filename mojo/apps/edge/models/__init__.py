"""
edge model exports.
"""

from .upstream import Upstream
from .vhost import Vhost
from .web_app_release import WebAppRelease
from .web_app import WebApp

__all__ = [
    "Upstream",
    "Vhost",
    "WebApp",
    "WebAppRelease",
]
