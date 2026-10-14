#!/usr/bin/env python
"""
Test runner bootstrap.
Activates the venv automatically if present, takes the run lock, flushes the
database, then hands off to testit.runner.
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

# The run lock comes before anything that can exit or reset test state (item
# #8052), including the runner import, which pulls in third-party packages. It
# marks var/test_failures.json "not completed" until this run replaces it, so
# no exit below can leave an older report looking current. A launch that
# cannot get the lock exits here having touched nothing — before this, it
# flushed the live run's database and Redis first.
#
# runlock is standard library only and is loaded by FILE PATH: importing the
# testit package would import helpers and its dependencies first. It is
# registered as testit.runlock, so the runner's own `from testit import
# runlock` gets this module and reuses the claim taken here.
def _load_runlock():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "testit.runlock", str(REPO_ROOT / "testit" / "runlock.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["testit.runlock"] = module
    spec.loader.exec_module(module)
    return module


# Launches that run no tests: they take the lock but write no marker. argparse
# accepts an abbreviation, so a prefix of 3+ characters counts. A prefix that
# is ambiguous for argparse (--h: --help/--host) is refused by claim_run with
# bad_arguments, which writes its own record, so treating it as a non-run here
# loses nothing. --list-tiers is --list-extras' alias.
_NON_RUN_OPTIONS = ("--help", "--init", "--list-extras", "--list-tiers")


def _is_non_run(argv):
    for arg in argv:
        if arg == "--":
            break
        if arg == "-h":
            return True
        name = arg.split("=", 1)[0]
        if name.startswith("--") and len(name) >= 3 and any(
                opt.startswith(name) for opt in _NON_RUN_OPTIONS):
            return True
    return False


def _refused_start(err, message):
    """A refused start never exits 0: print the cause, then re-raise anything
    but a successful SystemExit, which becomes exit 1."""
    print(f"ERROR:{message}", file=sys.stderr)
    if isinstance(err, SystemExit) and err.code in (0, None):
        raise SystemExit(1)
    raise err


runlock = _load_runlock()
_launch_claim, _holder = runlock.acquire(paths.VAR_ROOT)
if _launch_claim is None:
    print(runlock.holder_message(_holder, runlock.lock_path(paths.VAR_ROOT)))
    sys.exit(1)
_RUN_LAUNCH = not _is_non_run(sys.argv[1:])
if _RUN_LAUNCH:
    runlock.mark_pending(_launch_claim)

try:
    from testit import runner
except (Exception, SystemExit) as err:
    _message = f"  The launcher could not import the test runner: {err!r}"
    if _RUN_LAUNCH:
        runlock.refuse(_launch_claim, "runner_import", _message)
    else:
        runlock.release(_launch_claim)
    _refused_start(err, _message)

# Reuses the claim above. None means --init / --list-extras: not a test run,
# so nothing below may reset state.
claim = runner.claim_run(sys.argv[1:])

import django
try:
    django.setup()
except (Exception, SystemExit) as err:
    _message = f"  django.setup() failed: {err!r}"
    runner.refuse_run("django_setup", _message)
    _refused_start(err, _message)

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
            runner.refuse_run("redis_index_claimed", f"  {err}")
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
        runner.refuse_run(
            "pubsub_prefix",
            f"  REDIS_PUBSUB_PREFIX is {_actual_prefix or 'missing'!r}; this "
            f"checkout requires {_expected_prefix!r}. Run bin/create_testproject.")
        print(
            "ERROR: REDIS_PUBSUB_PREFIX is "
            + (f"'{_actual_prefix}'" if _actual_prefix else "missing")
            + f" but this checkout requires '{_expected_prefix}'.\n"
            "       The testproject predates Pub/Sub isolation or is stale.\n"
            "       Fix: run bin/create_testproject to regenerate it.",
            file=sys.stderr)
        sys.exit(1)

# Skip flush when continuing from a checkpoint so prior test state is preserved.
# Never flush without the run lock: --init and --list-extras run no tests.
if claim is None:
    pass
elif CONTINUING:
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

if __name__ == "__main__":
    runner.main(runner.setup_parser())
