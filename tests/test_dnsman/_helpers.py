"""Shared fixtures for the dnsman REST tests.

Filename starts with `_` so testit skips it during discovery.

These tests run against the LIVE dev server, so `mock.patch` in this process
does not reach the handler. Every fixture below is therefore built so the
endpoint under test refuses BEFORE any provider call: domains are left
non-active, or GoDaddy-backed with no usable credential, and purchasing stays
disabled by its default. No test here may reach AWS or GoDaddy.
"""

import uuid as _uuid


DNS_PERMS = ["view_dns", "manage_dns"]


def unique_email(prefix):
    return f"{prefix}_{_uuid.uuid4().hex[:8]}@dnsman.test"


def make_user(perms=None, is_superuser=False):
    """A verified, MFA-free login user with optional GLOBAL permissions."""
    from mojo.apps.account.models import User

    email = unique_email("dm")
    password = "Dm##dnsman99"
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


def make_group(prefix="dnsman"):
    from mojo.apps.account.models import Group

    return Group.objects.create(
        name=f"{prefix}_{_uuid.uuid4().hex[:8]}", kind="organization")


def make_group_member(perms, group=None):
    """A user holding `perms` ONLY at the GroupMember level."""
    from mojo.apps.account.models import User, GroupMember

    email = unique_email("dmmember")
    password = "Dm##member99"
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if group is None:
        group = make_group()
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {p: True for p in perms}
    member.save()
    return user, email, password, group


def login(opts, email, password):
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    ok = opts.client.login(email, password)
    assert ok, f"login failed for {email}: {opts.client.last_response.body}"


def make_domain(name=None, group=None, provider="godaddy", status="active",
                credential=None, user=None):
    """
    A Domain that is SAFE to point a REST test at.

    The default is GoDaddy-backed with no credential, so every DNS operation
    fails closed in `get_adapter` before a socket is opened.
    """
    from mojo.apps.dnsman.models import Domain

    if name is None:
        name = f"dm-{_uuid.uuid4().hex[:10]}.com"
    Domain.objects.filter(name=name).delete()
    return Domain.objects.create(
        name=name, group=group, user=user, provider=provider,
        status=status, credential=credential, verified=True)


def make_credential(group=None, verified=True, api_key="GDKEYtest1234",
                    api_secret="GDSECRETtest9876"):
    from mojo.apps.dnsman.models import DnsCredential

    credential = DnsCredential(
        name=f"cred-{_uuid.uuid4().hex[:8]}", group=group,
        provider="godaddy", verified=verified)
    credential.set_credentials(api_key, api_secret)
    credential.save()
    return credential


def make_certificate(domain, status="active"):
    """
    A Certificate row with NO key material.

    Deliberate: setting secrets would invoke KSMSecrets, which needs a KMS key
    the test environment does not have. With no secrets set, `save()` never
    calls KMS, and the material endpoint exercises its "custody unavailable"
    branch — which is the branch worth pinning anyway.
    """
    from mojo.apps.dnsman.models import Certificate

    return Certificate.objects.create(
        domain=domain, common_name=domain.name,
        sans=[domain.name, f"*.{domain.name}"], status=status)


# Field names that must never appear in a response. Checked as exact keys, not
# as substrings: `api_secret_masked` legitimately contains "api_secret", and a
# substring check would either flag it or force the marker list to be so loose
# it stops catching anything.
FORBIDDEN_KEYS = {
    "mojo_secrets", "api_key", "api_secret", "private_key_pem", "confirm_token",
}

# Literal fixture secrets. If one of these ever appears in a body, something
# serialized real key material regardless of what the field was called.
FORBIDDEN_VALUES = ["GDSECRETtest9876", "GDKEYtest1234"]


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            for found in _walk_keys(value):
                yield found
    elif isinstance(node, (list, tuple)):
        for item in node:
            for found in _walk_keys(item):
                yield found


def assert_no_secrets(body, where):
    """No response anywhere in this app may carry secret material."""
    for key in _walk_keys(body):
        assert key not in FORBIDDEN_KEYS, \
            f"{where}: response exposed the field {key!r}"
    blob = str(body)
    for value in FORBIDDEN_VALUES:
        assert value not in blob, \
            f"{where}: response leaked a raw secret value"
