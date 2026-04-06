"""
Built-in tool registration for the admin assistant.

Each tool module self-registers via the @tool decorator on import.
"""
from . import security  # noqa: F401
from . import jobs  # noqa: F401
from . import users  # noqa: F401
from . import groups  # noqa: F401
from . import metrics  # noqa: F401
from . import discovery  # noqa: F401
from . import web  # noqa: F401
from . import docs  # noqa: F401
from . import models  # noqa: F401
from . import logs  # noqa: F401
from . import files  # noqa: F401
from . import memory  # noqa: F401
from . import planning  # noqa: F401
