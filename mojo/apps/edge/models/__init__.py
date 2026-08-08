"""
edge model exports.
"""

from .upstream import Upstream
from .vhost import Vhost
from .route import VhostRoute
from .blocklist import BlocklistEntry
from .web_app_release import WebAppRelease
from .web_app import WebApp

__all__ = [
    "BlocklistEntry",
    "Upstream",
    "Vhost",
    "VhostRoute",
    "WebApp",
    "WebAppRelease",
]
