"""Installed vs published django-mojo, cached and pin-aware (maestro item #2146).

Three properties this service exists to hold:

- **PyPI is asked rarely.** The Admin dashboard reads it on every page load, so
  a cache miss is the only thing allowed to make a request.
- **It never raises.** No network, a 500, junk JSON, or a version string that
  is not a version all degrade to ``latest=None`` — a framework row that cannot
  answer must not take the whole dashboard down.
- **A pinned fleet is never offered an upgrade.** The deploy would not install
  it, so offering it is a lie.
"""

from unittest import mock

from testit import helpers as th


def _clear():
    from django.core.cache import cache
    from mojo.apps.edge.services import framework_version
    cache.delete(framework_version.CACHE_KEY)


def _response(version):
    payload = mock.Mock()
    payload.raise_for_status.return_value = None
    payload.json.return_value = {"info": {"version": version}}
    return payload


@th.django_unit_test("a cached PyPI answer is reused instead of asked again")
def test_cache_hit_makes_one_request(opts):
    from mojo.apps.edge.services import framework_version

    _clear()
    try:
        with mock.patch.object(framework_version.requests, "get",
                               return_value=_response("9.9.9")) as request:
            first = framework_version.status()
            second = framework_version.status()
        th.assert_eq(request.call_count, 1,
                     "the second status() asked PyPI again instead of the cache")
        th.assert_eq(first["source"], "pypi",
                     f"the first read is a live lookup: {first!r}")
        th.assert_eq(second["source"], "cache",
                     f"the second read must declare it came from cache: {second!r}")
        th.assert_eq(second["latest"], "9.9.9",
                     "the cached answer lost the published version")
    finally:
        _clear()


@th.django_unit_test("an unreachable PyPI degrades to no comparison, never an error")
def test_lookup_failure_is_absorbed(opts):
    from mojo.apps.edge.services import framework_version

    _clear()
    try:
        with mock.patch.object(framework_version.requests, "get",
                               side_effect=OSError("no route to host")):
            result = framework_version.status()
        th.assert_eq(result["latest"], None,
                     f"a failed lookup invented a published version: {result!r}")
        th.assert_eq(result["source"], "unavailable",
                     f"a failed lookup must say so: {result!r}")
        th.assert_eq(result["update_available"], False,
                     "an update was offered with nothing to compare against")
        th.assert_true(bool(result["installed"]),
                       "the installed version must be reported regardless")
    finally:
        _clear()


@th.django_unit_test("a version-shaped answer is required before it is believed")
def test_junk_version_rejected(opts):
    from mojo.apps.edge.services import framework_version

    _clear()
    try:
        with mock.patch.object(framework_version.requests, "get",
                               return_value=_response("not a version")):
            result = framework_version.status()
        th.assert_eq(result["latest"], None,
                     f"a non-version string was accepted as a release: {result!r}")
        th.assert_eq(result["update_available"], False,
                     "junk from PyPI produced an upgrade offer")
    finally:
        _clear()


@th.django_unit_test("versions compare numerically, so 1.10.0 beats 1.9.0")
def test_version_ordering(opts):
    from mojo.apps.edge.services import framework_version

    key = framework_version._version_key
    th.assert_true(key("1.10.0") > key("1.9.0"),
                   "1.10.0 sorted below 1.9.0 — a string compare leaked in")
    th.assert_true(key("1.12.3") > key("1.12.2"), "patch ordering is wrong")
    th.assert_true(key("2.0.0") > key("1.99.99"), "major ordering is wrong")
    th.assert_true(key("1.13.0rc1") > key("1.12.9"),
                   "a prerelease trailer must not discard the numeric prefix")
    th.assert_eq(key("garbage"), (),
                 "an unparsable version must compare as nothing, not raise")


@th.django_unit_test("a pinned fleet is never offered an upgrade it would not install")
def test_pinned_fleet_reports_without_offering(opts):
    from mojo.apps.edge.services import deploy, framework_version

    _clear()
    try:
        with mock.patch.object(framework_version.requests, "get",
                               return_value=_response("99.0.0")), \
                mock.patch.object(deploy, "framework_version_pin",
                                  return_value="1.11.6"):
            result = framework_version.status()
        th.assert_eq(result["pin"]["mode"], "pinned",
                     f"the operator's pin was not reported: {result!r}")
        th.assert_eq(result["latest"], "99.0.0",
                     "a pinned fleet must still be told what is published")
        th.assert_eq(result["update_available"], False,
                     "a pinned fleet was offered an upgrade a deploy would refuse")

        _clear()
        with mock.patch.object(framework_version.requests, "get",
                               return_value=_response("99.0.0")), \
                mock.patch.object(deploy, "framework_version_pin",
                                  return_value=deploy.FRAMEWORK_HOLD):
            held = framework_version.status()
        th.assert_eq(held["pin"]["mode"], "hold",
                     f"a hold was not reported as a hold: {held!r}")
        th.assert_eq(held["update_available"], False,
                     "a held fleet was offered an upgrade")

        _clear()
        with mock.patch.object(framework_version.requests, "get",
                               return_value=_response("99.0.0")), \
                mock.patch.object(deploy, "framework_version_pin",
                                  return_value=""):
            unpinned = framework_version.status()
        th.assert_eq(unpinned["pin"]["mode"], "latest",
                     f"an unset pin is the newest-release default: {unpinned!r}")
        th.assert_eq(unpinned["update_available"], True,
                     "an unpinned fleet behind PyPI was not offered the upgrade")
    finally:
        _clear()
