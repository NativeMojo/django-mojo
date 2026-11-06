"""The settings profile resolver and its opt-in production guard (#9315 Part A).

`resolve_profile(var_root, stderr=...)` reads `var_root/profile` exactly as
`load_settings_profile` always did (missing file: "local"). A deployment opts
in to refusing a wrong or missing marker by adding
`MOJO_REQUIRED_PROFILE = <name>` to its own `var/django.conf`. The key is read
with one anchored regex, never through the conf parser (which evals f-string
values), and a value that is not a plain profile name warns and leaves the
guard dormant. With no key the resolver is silent, whatever else is on disk.

Every test uses its own temp directory and an injected stderr; nothing shared
is touched.
"""
import io
import os
import tempfile
from testit import helpers as th

TESTIT_TIER = "framework"


def _tree(conf=None, profile=None, extra_files=()):
    root = tempfile.mkdtemp(prefix="mojo_profile_")
    if conf is not None:
        with open(os.path.join(root, "django.conf"), "w") as fh:
            fh.write(conf)
    if profile is not None:
        with open(os.path.join(root, "profile"), "w") as fh:
            fh.write(profile)
    for name in extra_files:
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x\n")
    return root


def _resolve(root):
    from pathlib import Path
    from mojo.helpers.settings.helper import resolve_profile
    err = io.StringIO()
    return resolve_profile(Path(root), stderr=err), err.getvalue()


def _refusal(root):
    from pathlib import Path
    from django.core.exceptions import ImproperlyConfigured
    from mojo.helpers.settings.helper import resolve_profile
    try:
        resolve_profile(Path(root), stderr=io.StringIO())
    except ImproperlyConfigured as exc:
        return str(exc)
    return None


@th.django_unit_test("profile guard: key prod + no marker refuses, naming both paths and the fix")
def test_key_without_marker_refuses(opts):
    root = _tree(conf="MOJO_REQUIRED_PROFILE = prod\n")
    msg = _refusal(root)
    assert msg is not None, "a required profile with no marker must refuse to start"
    assert "django-mojo refused to start" in msg, f"the refusal must say so plainly: {msg}"
    assert os.path.join(root, "profile") in msg, f"the refusal must name the marker path: {msg}"
    assert os.path.join(root, "django.conf") in msg, f"the refusal must name the conf path: {msg}"
    assert "missing" in msg, f"the refusal must say what was found: {msg}"
    assert "chmod 640" in msg and "mojo-asgi" in msg, f"the refusal must give the fix: {msg}"


@th.django_unit_test("profile guard: key prod + empty or other marker refuses")
def test_key_with_wrong_marker_refuses(opts):
    for marker, found in (("", "empty"), ("\n", "empty"), ("local\n", "local")):
        root = _tree(conf="MOJO_REQUIRED_PROFILE = prod\n", profile=marker)
        msg = _refusal(root)
        assert msg is not None, f"marker {marker!r} must refuse when prod is required"
        assert found in msg, f"the refusal must say what it found ({found}): {msg}"


@th.django_unit_test("profile guard: key matches marker resolves to it")
def test_key_matching_marker_resolves(opts):
    profile, err = _resolve(_tree(conf="MOJO_REQUIRED_PROFILE = prod\n", profile="prod\n"))
    assert profile == "prod", f"matching marker must resolve to prod, got {profile!r}"
    assert err == "", f"a satisfied guard writes nothing, got {err!r}"
    profile, err = _resolve(_tree(conf="MOJO_REQUIRED_PROFILE = staging\n", profile="staging"))
    assert profile == "staging", f"a staging requirement must resolve to staging, got {profile!r}"


@th.django_unit_test("profile guard: no key + no marker is today's behaviour, silently")
def test_no_key_is_todays_behaviour(opts):
    profile, err = _resolve(_tree())
    assert profile == "local", f"no marker and no conf must resolve to local, got {profile!r}"
    assert err == "", f"no key must write nothing, got {err!r}"
    profile, err = _resolve(_tree(conf="DEBUG = False\n"))
    assert profile == "local", f"no key must resolve to local, got {profile!r}"
    assert err == "", f"no key must write nothing, got {err!r}"
    profile, err = _resolve(_tree(conf="DEBUG = False\n", profile="prod\n"))
    assert profile == "prod", f"no key leaves the marker in charge, got {profile!r}"


@th.django_unit_test("profile guard: no key + deploy evidence stays silent (Rick's amendment)")
def test_no_key_with_deploy_evidence_is_silent(opts):
    for evidence in ("bootstrap.conf", "deploy/systemd/mojo-asgi.service", "app_sha"):
        profile, err = _resolve(_tree(conf="DEBUG = False\n", extra_files=(evidence,)))
        assert profile == "local", f"{evidence} must not change the resolved profile, got {profile!r}"
        assert err == "", f"{evidence} must not produce output without the key, got {err!r}"


@th.django_unit_test("profile guard: quoted, commented and repeated keys")
def test_key_spellings(opts):
    for conf in ('MOJO_REQUIRED_PROFILE = "prod"\n', "MOJO_REQUIRED_PROFILE='prod'\n",
                 "  MOJO_REQUIRED_PROFILE=prod  \n"):
        assert _refusal(_tree(conf=conf)) is not None, f"{conf!r} must arm the guard"
    assert _resolve(_tree(conf="# MOJO_REQUIRED_PROFILE = prod\n"))[0] == "local", \
        "a commented key must be ignored"
    assert _refusal(_tree(conf="MOJO_REQUIRED_PROFILE = prod\nMOJO_REQUIRED_PROFILE = local\n",
                          profile="local\n")) is None, \
        "the last occurrence must win (local is required and the marker reads local)"
    assert _refusal(_tree(conf="MOJO_REQUIRED_PROFILE = local\nMOJO_REQUIRED_PROFILE = prod\n",
                          profile="local\n")) is not None, \
        "the last occurrence must win (prod is required and the marker reads local)"


@th.django_unit_test("profile guard: a malformed value warns once and stays dormant")
def test_malformed_value_is_dormant(opts):
    for conf in ("MOJO_REQUIRED_PROFILE =\n", 'MOJO_REQUIRED_PROFILE = ""\n',
                 'MOJO_REQUIRED_PROFILE = f"{1/0}"\n', "MOJO_REQUIRED_PROFILE = [prod]\n",
                 "MOJO_REQUIRED_PROFILE = prod staging\n"):
        profile, err = _resolve(_tree(conf=conf))
        assert profile == "local", f"{conf!r} must leave the guard dormant, got {profile!r}"
        assert err.count("\n") == 1 and "MOJO_REQUIRED_PROFILE" in err, \
            f"{conf!r} must write exactly one warning line naming the key, got {err!r}"
        assert "1/0" not in err, f"the warning must not echo the value: {err!r}"


@th.django_unit_test("profile guard: the conf is never evaluated")
def test_conf_is_never_evaluated(opts):
    conf = 'BAD = f"{1/0}"\nMOJO_REQUIRED_PROFILE = prod\n'
    profile, err = _resolve(_tree(conf=conf, profile="prod\n"))
    assert profile == "prod", f"a conf with an exploding f-string must still resolve, got {profile!r}"
    assert err == "", f"nothing must be written, got {err!r}"


@th.django_unit_test("profile guard: an unreadable conf counts as no key")
def test_unreadable_conf_is_no_key(opts):
    root = _tree()
    os.makedirs(os.path.join(root, "django.conf"))  # a directory cannot be read as text
    profile, err = _resolve(root)
    assert profile == "local", f"an unreadable conf must count as no key, got {profile!r}"
    assert err == "", f"an unreadable conf must write nothing, got {err!r}"
