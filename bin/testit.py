#!/usr/bin/env python
"""
Test runner bootstrap.
Activates the venv automatically if present, flushes the database,
then hands off to testit.runner.
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent   # django-mojo/bin/
REPO_ROOT = SCRIPT_DIR.parent                  # django-mojo/

# Auto-activate venv if not already in one
if sys.prefix == sys.base_prefix:
    import shutil
    uv = shutil.which("uv")
    if uv:
        os.execv(uv, [uv, "run", "python"] + sys.argv)
    else:
        venv_python = REPO_ROOT / ".venv" / "bin" / "python"
        if venv_python.exists():
            os.execv(str(venv_python), [str(venv_python)] + sys.argv)
TESTPROJECT = REPO_ROOT / "testproject"

if not TESTPROJECT.exists():
    print("ERROR: testproject/ not found. Run ./bin/create_testproject first.")
    sys.exit(1)

sys.path.insert(0, str(REPO_ROOT))                      # mojo, testit
sys.path.insert(0, str(TESTPROJECT / "config"))         # settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

from mojo.helpers import paths
paths.configure_paths(str(TESTPROJECT / "config" / "settings" / "__init__.py"), 1)

import django
django.setup()

def _load_testenv():
    """Load testit/testenv.py by FILE PATH, the way bin/create_testproject does.

    Importing the package runs testit/__init__.py and everything under it; this
    guard needs one module and nothing else.
    """
    import importlib.util

    path = REPO_ROOT / "testit" / "testenv.py"
    spec = importlib.util.spec_from_file_location("mojo_testenv", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_redis_index(client):
    """Which database the client we are about to flush is actually pointed at.

    Read off the LIVE client, never off settings: mojo.helpers.redis gives
    REDIS_URL precedence over REDIS_DB_INDEX, and adopting repos set REDIS_URL.
    A guard that protects index N while flushdb() empties index M is worse than
    no guard at all.

    The same argument applies to the HOST, which is why the client itself is
    handed to testenv below rather than only this number: testenv's own probe
    resolves host and port from MOJO_TESTENV_REDIS_HOST/_PORT (localhost by
    default), independently of REDIS_URL, so a number alone would have it guard
    index N on one machine while the flush empties index N on another.
    """
    pool = getattr(client, "connection_pool", None)
    if pool is None:
        return None
    return getattr(pool, "connection_kwargs", {}).get("db", 0)


CONTINUING = "--continue" in sys.argv

from mojo.helpers.redis import get_connection
redis_client = get_connection()

if redis_client is not None:
    # Refuse to touch a Redis index another live checkout has stamped. Checked
    # on --continue too: that path skips the flush, but skipping the guard is
    # what would let it overwrite somebody else's stamp.
    #
    # The live client goes in with it. That is what makes the guard read the
    # exact server+database flushdb() is about to empty, and it is also why
    # MOJO_TESTENV_NO_REDIS cannot disarm the guard here: that variable turns
    # off testenv's own probe, and with a client supplied there is no probe.
    testenv = _load_testenv()
    redis_index = _live_redis_index(redis_client)
    if redis_index is None:
        # A cluster client has no single database to guard. Say so rather than
        # letting a missing guard look like a passing one.
        print("==> WARNING: cannot determine the Redis database index; "
              "the ownership guard is not active")
    else:
        try:
            testenv.claim_redis_index(
                redis_index, str(REPO_ROOT), client=redis_client)
        except testenv.AllocationError as err:
            print(f"ERROR: {err}", file=sys.stderr)
            sys.exit(1)

    # Pub/Sub isolation guard. The db index above only isolates stored keys —
    # Redis Pub/Sub ignores database numbers — so an isolated run must carry
    # the per-checkout channel prefix, or its realtime messages and job
    # broadcasts land on the legacy shared channels of every other checkout.
    # A testproject generated before the prefix existed fails here visibly
    # instead of falling back silently.
    from mojo.helpers.settings import settings as mojo_settings
    _expected_prefix = testenv.expected_pubsub_prefix(str(REPO_ROOT))
    _actual_prefix = mojo_settings.get_static("REDIS_PUBSUB_PREFIX", "") or ""
    if _actual_prefix != _expected_prefix:
        print(
            "ERROR: REDIS_PUBSUB_PREFIX is "
            + (f"'{_actual_prefix}'" if _actual_prefix else "missing")
            + f" but this checkout requires '{_expected_prefix}'.\n"
            "       The testproject predates Pub/Sub isolation or is stale.\n"
            "       Fix: run bin/create_testproject to regenerate it.",
            file=sys.stderr)
        sys.exit(1)

# Skip flush when continuing from a checkpoint so prior test state is preserved
if CONTINUING:
    print("==> Skipping flush (--continue mode)")
else:
    from django.db import connection
    print("==> Flushing database and Redis")
    with connection.cursor() as cursor:
        # One TRUNCATE statement, not one per table. django_migrations is
        # excluded (maestro #2789): wiping it forced every server start down
        # the "tracker empty" path — migrate --fake + makemigrations + migrate,
        # three Django boots — instead of a single no-op migrate. Data tables
        # still reset every run; the migration record is not test state.
        cursor.execute("""
            DO $$ DECLARE tables TEXT;
            BEGIN
                SELECT string_agg(quote_ident(tablename), ', ') INTO tables
                FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename <> 'django_migrations';
                IF tables IS NOT NULL THEN
                    EXECUTE 'TRUNCATE TABLE ' || tables || ' CASCADE';
                END IF;
            END $$;
        """)

    if redis_client is not None:
        redis_client.flushdb()
        # The flush destroyed the stamp along with everything else. Re-stamp,
        # but ONLY here: a SET on the --continue path would have no preceding
        # claim behind it and could quietly take over another checkout's index.
        # Same client again: stamping through a probe would write the claim
        # onto a localhost index this checkout never uses.
        testenv.stamp_redis_owner(
            redis_index, str(REPO_ROOT), client=redis_client)

from testit import runner

if __name__ == "__main__":
    runner.main(runner.setup_parser())
