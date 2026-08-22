__version__ = "1.16.1"

# THIS LINE IS ON THE NODE-BOOTSTRAP PATH AND MUST STAY SETTINGS-FREE.
# `python3 -m mojo.deploy.config_sync` imports this package before it can parse
# a single line of bootstrap.conf, and it runs before django.conf exists at all.
# If anything reachable from mojo.helpers.response starts reading Django
# settings at import time, config fetch breaks on every node of every project
# at once. See mojo/deploy/__init__.py and tests/test_deploy/import_isolation.py.
from mojo.helpers.response import JsonResponse
