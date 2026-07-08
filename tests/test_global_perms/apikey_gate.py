"""A group-scoped ApiKey must not satisfy a platform-global gate.

requires_global_perms rejects non-User identities by default (an ApiKey is a
group credential; letting it pass recreates the escalation through a machine
door). The lone exception is allow_api_keys=True on the geoip/sync federation
receiver, whose intended caller IS a fleet-peer ApiKey — verified here too.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_global_perms._helpers import use_apikey


@th.django_unit_setup()
def setup_apikey_gate(opts):
    from mojo.apps.account.models import Group, ApiKey

    ApiKey.objects.filter(name__startswith="gp_apikey_test_").delete()
    Group.objects.filter(name__startswith="gp_apikey_group_").delete()

    group = Group.objects.create(name=f"gp_apikey_group_{_uuid.uuid4().hex[:8]}", kind="organization")
    # A key holding perms that gate global endpoints — must STILL be rejected
    # by the default global gate.
    key_admin, token_admin = ApiKey.create_for_group(
        group=group, name="gp_apikey_test_admin",
        permissions={"manage_jobs": True, "jobs": True, "manage_geofence": True,
                     "view_geofence": True, "security": True, "geoip_sync": True,
                     "manage_aws": True, "comms": True,
                     "manage_users": True, "manage_devices": True, "users": True},
    )
    opts.group_id = group.pk
    opts.token_admin = token_admin


@th.django_unit_test("apikey gate: a group ApiKey cannot pass a global-only gate")
def test_apikey_denied_on_global_endpoints(opts):
    use_apikey(opts, opts.token_admin)
    try:
        for method, path in [
            ("GET", "/api/jobs/control/config"),
            ("GET", "/api/geo/rules"),
            ("POST", "/api/geo/rules"),
            # Groupless RestMeta models reached via delegating requires_perms
            # endpoints: a self-minted group key with manage_aws / manage_users
            # would otherwise LIST them cross-tenant through the model layer's
            # api_key branch (rest.py:288). The global gate must reject the key
            # BEFORE on_rest_request runs.
            ("GET", "/api/aws/email/domain"),
            ("GET", "/api/aws/email/mailbox"),
            ("GET", "/api/aws/email/template"),
            ("GET", "/api/aws/email/incoming"),
            ("GET", "/api/aws/email/sent"),
            ("GET", "/api/user/device/location"),
        ]:
            if method == "GET":
                resp = opts.client.get(path)
            else:
                resp = opts.client.post(path, {"rule": {}})
            assert resp.status_code in (401, 403), \
                f"ApiKey must be denied on {path}, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()


@th.django_unit_test("apikey gate: allow_api_keys endpoint (geoip/sync) still accepts a key")
def test_apikey_allowed_on_geoip_sync(opts):
    """geoip/sync uses allow_api_keys=True — a key holding geoip_sync is the
    intended federation caller and must be accepted (not swept up by the guard)."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    ip = f"203.0.113.{(int(_uuid.uuid4().hex[:4], 16) % 200) + 20}"
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    use_apikey(opts, opts.token_admin)
    try:
        resp = opts.client.post("/api/system/geoip/sync", {"ip": ip, "threat_level": "high"})
        assert resp.status_code == 200, \
            f"federation ApiKey with geoip_sync must be accepted, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
        GeoLocatedIP.objects.filter(ip_address=ip).delete()
