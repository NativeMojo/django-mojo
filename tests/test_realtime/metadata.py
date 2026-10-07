
TESTIT_TIER = "bug"
from testit import helpers as th

# Regression tests for maestro item 74: the realtime server holds a
# LONG-LIVED user instance per socket, and a whole-field save of that stale
# snapshot silently reverts any metadata written (e.g. via REST) while the
# socket is open. These tests simulate the two-instance race without a real
# websocket: "ws_user" is the instance the realtime server pins at auth;
# "rest_user" is a concurrent writer.
#
# The connect/disconnect half of #74 is gone at the root: since #6562 a
# connect or disconnect writes nothing to the User row at all — see
# keepalive.py, test_connect_and_disconnect_leave_the_user_row_alone.

TEST_USER = "ws_meta_clobber"


@th.django_unit_setup()
def setup_metadata_user(opts):
    from mojo.apps.account.models import User
    # tests run on long-lived databases — clean up before creating
    User.objects.filter(username=TEST_USER).delete()
    user = User(
        username=TEST_USER,
        display_name=TEST_USER,
        email=f"{TEST_USER}@example.com")
    user.metadata = {"portal": {"fav_workspaces": [1]}}
    user.save()
    opts.meta_user_id = user.id


def _concurrent_portal_write(user_id, favs):
    """Simulate a REST metadata write landing while the socket is open."""
    from mojo.apps.account.models import User
    rest_user = User.objects.get(pk=user_id)
    meta = rest_user.metadata or {}
    meta.setdefault("portal", {})["fav_workspaces"] = favs
    rest_user.metadata = meta
    rest_user.save(update_fields=["metadata"])


@th.django_unit_test("realtime_set_meta_leaves_metadata_alone")
def test_set_meta_leaves_metadata_alone(opts):
    # The set_meta message was removed (maestro #6226): any socket could write
    # any metadata key with it. A stale socket instance that receives one must
    # write nothing at all, so a concurrent write survives untouched.
    from mojo.apps.account.models import User
    ws_user = User.objects.get(pk=opts.meta_user_id)
    _concurrent_portal_write(opts.meta_user_id, [4321])
    result = ws_user.on_realtime_message(
        {"type": "set_meta", "key": "theme", "value": "dark"})
    response = (result or {}).get("response") or {}
    assert "key" not in response, f"set_meta acknowledged a write: {result!r}"

    fresh = User.objects.get(pk=opts.meta_user_id)
    meta = fresh.metadata or {}
    assert meta.get("portal", {}).get("fav_workspaces") == [4321], (
        f"a set_meta message clobbered a concurrent metadata write: "
        f"expected portal.fav_workspaces == [4321], got {meta!r}")
    assert "theme" not in meta, f"set_meta wrote a metadata key: {meta!r}"
