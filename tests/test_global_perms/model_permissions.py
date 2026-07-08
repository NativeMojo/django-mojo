"""Regression for the model_permissions 500: the two endpoints passed a LIST
LITERAL to @md.requires_perms — `@md.requires_perms(['view_admin', ...])` — so
at request time `set(required_perms)` became `set(([...],))` and raised
`TypeError: unhashable type: 'list'`, 500-ing before authorizing.

The module is not auto-mounted in the testproject (it lives under mojo/rest/,
not an installed app's rest/ package), so there is no HTTP route to exercise.
This asserts the decorator metadata is now a FLAT perm list (varargs), which is
exactly what the buggy nested-list form got wrong — a flat list can be set()'d.
"""
from testit import helpers as th


@th.unit_test("model_permissions: decorators use flat varargs perms, not a nested list")
def test_flat_perm_metadata(opts):
    import mojo.rest.model_permissions as mp

    for fn_name in ("rest_model_permissions", "rest_model_permission_detail"):
        fn = getattr(mp, fn_name)
        perms = getattr(fn, "_mojo_required_permissions", None)
        assert perms == ["view_admin", "manage_users", "admin"], \
            f"{fn_name} perms must be a flat list, got {perms!r}"
        # The bug was an unhashable nested list; prove the perms are set()-able.
        try:
            set(perms)
        except TypeError as exc:
            assert False, f"{fn_name} perms are not hashable (the 500 bug): {exc}"
        # And confirm it went through the global-only gate (no group fallback).
        entry = None
        from mojo.decorators.auth import SECURITY_REGISTRY
        for key, e in SECURITY_REGISTRY.items():
            if key.endswith(f".{fn_name}"):
                entry = e
                break
        assert entry is not None, f"{fn_name} missing from SECURITY_REGISTRY"
        assert entry.get("global_only") is True, \
            f"{fn_name} must use the global-only gate, got {entry}"
