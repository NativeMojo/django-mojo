"""Node deployment tooling that ships with django-mojo.

Everything in this package runs BEFORE Django settings exist — `config_sync`
is the thing that puts `django.conf` on disk, so it cannot read it — and is
invoked with `python3 -m mojo.deploy.<module>` rather than through
`manage.py`.

The modules:

    config_sync    pull this node's django.conf from S3 (systemd oneshot+timer)
    check_setup    read-only AWS account audit (topology, security, durability)
    certbot_sync   share a Let's Encrypt lineage across a fleet via S3
    check_node     read-only audit of ONE node against the deploy contract
    jobman         start/stop/status for the foreground job engine + scheduler
    node_setup     converge var/ ownership, systemd units, and the jobs cron

Plus the package entry `python3 -m mojo.deploy` (see `__main__.py`):

    locate <name>  print the abs path of a packaged node script
                   (update.sh, post_deploy.sh) for the project shims to exec
    render ...     materialize templates/{cron.d,systemd} into
                   ${PROJ_PATH}/var/deploy with the node's parameters, plus
                   the project's aws/ overlays (node_overrides.conf policy)

The non-Python payload lives in `scripts/` (the two bash node scripts) and
`templates/` (cron.d + systemd) — shipped inside the wheel, resolved relative
to this package's `__file__`.

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
