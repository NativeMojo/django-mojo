"""Node deployment tooling that ships with django-mojo.

Everything in this package runs BEFORE Django settings exist — `config_sync`
is the thing that puts `django.conf` on disk, so it cannot read it — and is
invoked with `python3 -m mojo.deploy.<module>` rather than through
`manage.py`.

THE CONTRACT FOR THIS PACKAGE:

    - No Django. Nothing here may import `django.conf.settings` or anything
      that reads it at import time.
    - `mojo.helpers.*` is OFF-LIMITS. `mojo/helpers/logit.py` reads
      `paths.LOG_ROOT` at module level and `paths.py` only creates that
      attribute inside `configure_paths()`, so `from mojo.helpers import logit`
      raises AttributeError with no settings configured. The same chain kills
      all of `mojo.helpers.aws`, so `get_client()` / `get_session()` are
      unusable here — build the boto3 client directly. Deferring the import
      into a function does not help; it fails at call time instead.
    - This module itself imports nothing, deliberately, so that
      `python3 -m mojo.deploy.config_sync` pays for the parent package and
      nothing else.

See `docs/django_developer/deploy/README.md`.
"""
