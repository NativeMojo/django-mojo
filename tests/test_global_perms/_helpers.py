"""Shared helpers for the global-permission escalation tests.

Filename starts with `_` so testit skips it during discovery.
"""
import uuid as _uuid


# Every permission name used by any endpoint in the escalation sweep. The
# member user is granted ALL of these at the GroupMember level — the whole
# point is to prove that a group-scoped grant of these perms does NOT authorize
# the global-effect endpoints.
ALL_ENDPOINT_PERMS = [
    "manage_jobs", "view_jobs", "jobs",
    "manage_aws", "comms", "files",
    "manage_users", "manage_devices", "users", "security",
    "send_notifications", "manage_push_config",
    "view_security", "manage_incidents", "metrics", "manage_metrics",
    "view_admin", "assistant",
    "geoip_sync",
    "view_geofence", "manage_geofence",
]


def unique_email(prefix):
    return f"{prefix}_{_uuid.uuid4().hex[:8]}@globalperms.test"


def make_user(perms=None, is_superuser=False):
    """Create a verified, MFA-free login user with optional GLOBAL perms."""
    from mojo.apps.account.models import User
    email = unique_email("gp")
    password = "Gp##perms99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    if is_superuser:
        user.is_superuser = True
    user.save()
    if perms:
        user.add_permission(list(perms))
        user.save()
    return user, email, password


def make_group_member(perms):
    """Create a user who holds `perms` ONLY at the GroupMember level (no global
    grants), plus their group. Returns (user, email, password, group)."""
    from mojo.apps.account.models import User, Group, GroupMember
    email = unique_email("gpmember")
    password = "Gp##member99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    group = Group.objects.create(name=f"gp_group_{_uuid.uuid4().hex[:8]}", kind="organization")
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {p: True for p in perms}
    member.save()
    return user, email, password, group


def login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def use_apikey(opts, token):
    """Switch the test client to `Authorization: apikey <token>` (mirrors
    tests/test_account/test_geoip_sync_endpoint.py)."""
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True
