"""Tests for GeoLocatedIP POST_SAVE_ACTIONS dispatch (whitelist, unblock, etc.)."""
from testit import helpers as th


@th.django_unit_test()
def test_post_save_actions_is_flat_list_of_strings(opts):
    """
    Regression: a stray trailing comma on POST_SAVE_ACTIONS turned it into a
    1-tuple containing a list, so `"whitelist" in POST_SAVE_ACTIONS` was always
    False and the REST dispatcher skipped all on_action_* handlers silently.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    actions = GeoLocatedIP.RestMeta.POST_SAVE_ACTIONS
    assert isinstance(actions, list), (
        f"POST_SAVE_ACTIONS must be a list, got {type(actions).__name__}: {actions!r}"
    )
    for name in ("refresh", "threat_analysis", "block", "unblock", "whitelist", "unwhitelist"):
        assert name in actions, (
            f"action {name!r} missing from POST_SAVE_ACTIONS: {actions!r}"
        )


@th.django_unit_test()
def test_whitelist_action_dispatches_to_handler(opts):
    """PUT-style save dict with {'whitelist': reason} routes to on_action_whitelist."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from objict import objict

    GeoLocatedIP.objects.filter(ip_address="203.0.113.201").delete()
    geo = GeoLocatedIP.objects.create(ip_address="203.0.113.201", provider="test")
    assert geo.is_whitelisted is False, "fixture must start unwhitelisted"

    fake_user = objict(is_authenticated=False, username="tester")
    request = objict(user=fake_user, group=None, member=None)

    geo.on_rest_save(request, {"whitelist": "Known office ip"})
    geo.refresh_from_db()

    assert geo.is_whitelisted is True, (
        f"whitelist action did not set is_whitelisted; got is_whitelisted={geo.is_whitelisted!r}"
    )
    assert geo.whitelisted_reason and "Known office ip" in geo.whitelisted_reason, (
        f"whitelist reason not stored; got {geo.whitelisted_reason!r}"
    )


@th.django_unit_test()
def test_unblock_action_dispatches_to_handler(opts):
    """{'unblock': reason} routes to on_action_unblock and clears is_blocked."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers import dates
    from objict import objict

    GeoLocatedIP.objects.filter(ip_address="203.0.113.202").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.202",
        provider="test",
        is_blocked=True,
        blocked_at=dates.utcnow(),
        blocked_reason="test",
    )

    fake_user = objict(is_authenticated=False, username="tester")
    request = objict(user=fake_user, group=None, member=None)

    geo.on_rest_save(request, {"unblock": "cleared by test"})
    geo.refresh_from_db()

    assert geo.is_blocked is False, (
        f"unblock action did not clear is_blocked; got is_blocked={geo.is_blocked!r}"
    )
