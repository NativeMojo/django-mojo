"""Host collectors included in the deliberately small MojoSec v1 profile."""

from .fim import FimCollector
from .journal import JournalCollector
from .nginx import NginxCollector

__all__ = ["FimCollector", "JournalCollector", "NginxCollector"]

