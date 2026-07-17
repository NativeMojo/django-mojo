"""DM-044 regression — stacked auth decorators must MERGE into SECURITY_REGISTRY.

Ten decorators in mojo/decorators/auth.py used to register endpoints with a
full-dict overwrite (SECURITY_REGISTRY[key] = {...}). Decorators apply
bottom-up, so any of them sitting ABOVE @requires_geofence registered later
and wiped the `geofence` sub-entry — silently dropping most geofenced
endpoints from the GET /api/geo/rules enforced_endpoints compliance artifact.
All registration sites must merge into the existing entry instead.

In-process tests: they inspect the registry populated by importing the rest
modules into THIS process (like post_auth.test_registry_annotates_after_auth).
"""
from testit import helpers as th


@th.django_unit_test("registry: public_endpoint above requires_geofence keeps both (DM-044)")
def test_stacked_decorators_merge_registry(opts):
    """The on_register shape: @public_endpoint stacked above @requires_geofence.
    Both decorators' registry info must survive, in both directions."""
    from mojo import decorators as md
    from mojo.decorators.auth import SECURITY_REGISTRY

    @md.public_endpoint("DM-044 stacked probe")
    @md.requires_geofence(scope="auth")
    def dm044_stacked_probe(request):
        pass

    key = f"{dm044_stacked_probe.__module__}.{dm044_stacked_probe.__name__}"
    entry = SECURITY_REGISTRY.get(key)
    assert entry is not None, f"probe must be registered under {key}"

    # geofence info written first (bottom decorator) must survive the
    # public_endpoint registration applied above it.
    gf = entry.get("geofence")
    assert gf is not None, \
        f"geofence sub-entry must survive a later public_endpoint registration, got {entry}"
    assert gf.get("scope") == "auth", f"geofence scope must survive, got {gf}"

    # ...and the public_endpoint info must be present too (merge, not replace).
    assert entry.get("type") == "public", \
        f"public_endpoint type must be present alongside geofence, got {entry}"
    assert entry.get("requires_auth") is False, \
        f"public_endpoint requires_auth=False must be present, got {entry}"

    # The probe must therefore surface in the compliance artifact.
    from mojo.apps.account.rest.geofence import _enforced_endpoints
    listed = {e["endpoint"] for e in _enforced_endpoints()}
    assert key in listed, \
        "stacked probe must appear in enforced_endpoints regardless of decorator order"


@th.django_unit_test("registry: real on_register appears in enforced_endpoints (DM-044)")
def test_on_register_in_enforced_endpoints(opts):
    """The live victim: user.py's on_register has @public_endpoint above
    @requires_geofence(scope='auth') and used to vanish from the artifact."""
    # Importing the rest module populates SECURITY_REGISTRY in-process.
    import mojo.apps.account.rest.user  # noqa: F401
    from mojo.apps.account.rest.geofence import _enforced_endpoints

    entries = {e["endpoint"]: e for e in _enforced_endpoints()}
    register = next(
        (e for k, e in entries.items() if k.endswith("rest.user.on_register")), None)
    assert register is not None, \
        "on_register (@public_endpoint over @requires_geofence) must be listed in enforced_endpoints"
    assert register.get("scope") == "auth", \
        f"on_register geofence scope must be 'auth', got {register}"
