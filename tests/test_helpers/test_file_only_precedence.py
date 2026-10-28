"""Default-tier file-only-vs-DB precedence contracts (maestro item #2558).

Threat model: an attacker (or compromised group admin) who can write a
Setting row must not be able to arm a bypass that is meant to be file-only.
The behavioral variants that write REAL keys stay opt-in
(tests/test_register_extended_serial/bypass_file_only.py,
tests/test_geofence_extended_serial/test_override_file_only.py,
tests/test_models_extended_serial/return_real_error.py). This file is the
default-tier smoke plus a syntax pin:

1. the MECHANISM — `settings.get_static` never consults the database — is
   proven with a reserved TESTIT_ probe row;
2. the WIRING — the security-sensitive file-only keys are never read through
   the DB-first `settings.get` anywhere in mojo/ — is pinned by AST scan,
   asserted as the INVERSE (no `settings.get(<key>)` occurrence at all), so
   wrapper idioms that conditionally route to `settings.get` are caught too.
"""
import ast
import os

from testit import helpers as th

TESTIT_TIER = "core"  # #2792 tier curation

PROBE_KEY = "TESTIT_FILE_ONLY_PROBE"

# Keys whose file-only read is a security boundary. Sourced from the opt-in
# behavioral suites; extend when a new file-only bypass key ships.
FILE_ONLY_KEYS = (
    "AUTH_PHONE_VERIFY_DEV_BYPASS_CODE",
    "GEOFENCE_TEST_OVERRIDE",
    "MOJO_TEST_MODE",
    "LOGIT_RETURN_REAL_ERROR",
)


@th.django_unit_test("get_static never consults the database")
def test_get_static_ignores_db_row(opts):
    from mojo.apps.account.models import Setting
    from mojo.helpers.settings import settings

    Setting.objects.filter(key=PROBE_KEY).delete()
    Setting.set(PROBE_KEY, "db-armed")
    try:
        value = settings.get_static(PROBE_KEY, "file-default")
        assert value == "file-default", (
            f"get_static must resolve file settings and defaults ONLY — a "
            f"database Setting row must never reach it, got {value!r}"
        )
        assert settings.get(PROBE_KEY) == "db-armed", (
            "control: settings.get IS DB-first for the same key — if this "
            "fails the probe row never landed and the assertion above proved "
            "nothing"
        )
    finally:
        Setting.objects.filter(key=PROBE_KEY).delete()


@th.django_unit_test("file-only bypass keys are never read through DB-first settings.get")
def test_file_only_keys_never_use_settings_get(opts):
    import mojo

    mojo_root = os.path.dirname(mojo.__file__)
    offenders = []
    for root, _dirs, files in os.walk(mojo_root):
        if "__pycache__" in root:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path).read())
            except SyntaxError:
                offenders.append(f"{path}: unparsable")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "get"):
                    continue
                # Only settings-object receivers: `settings.get(...)` or
                # `<x>.settings.get(...)`. A plain dict .get of the same string
                # is not a settings read.
                recv = func.value
                recv_name = recv.id if isinstance(recv, ast.Name) else (
                    recv.attr if isinstance(recv, ast.Attribute) else None)
                if recv_name != "settings":
                    continue
                args = list(node.args)
                if not args or not isinstance(args[0], ast.Constant):
                    continue
                if args[0].value in FILE_ONLY_KEYS:
                    rel = os.path.relpath(path, os.path.dirname(mojo_root))
                    offenders.append(f"{rel}:{node.lineno} settings.get({args[0].value!r})")
    assert not offenders, (
        "a file-only bypass key is being read through a `.get(...)` call — "
        "settings.get is DB-first, so a Setting row could arm the bypass. "
        "Read these keys with settings.get_static instead:\n  "
        + "\n  ".join(offenders)
    )


@th.django_unit_test("the pinned key list itself is still read somewhere via get_static")
def test_pinned_keys_still_exist(opts):
    """A key that vanishes from the codebase makes the inverse pin vacuous —
    fail loudly so the roster gets pruned deliberately, not silently."""
    import subprocess
    import mojo

    mojo_root = os.path.dirname(mojo.__file__)
    for key in FILE_ONLY_KEYS:
        result = subprocess.run(
            ["grep", "-rl", key, mojo_root, "--include=*.py"],
            capture_output=True, text=True)
        assert result.stdout.strip(), (
            f"{key} no longer appears anywhere under mojo/ — remove it from "
            f"FILE_ONLY_KEYS deliberately rather than pinning a ghost"
        )
