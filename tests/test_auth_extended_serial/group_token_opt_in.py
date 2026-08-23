"""The grouptoken auth scheme is opt-in per deployment (maestro #2791).

Moved from tests/test_auth/group_token.py: AUTH_BEARER_HANDLERS is read at
module load (settings.get_static in mojo/middleware/auth.py), so unregistering
the handler to prove the scheme is opt-in genuinely needs a server reload —
legal only in this serial/opt-in package. A minimal self-contained setup mints
one group token; the exhaustive grouptoken confinement matrix stays in the
default-tier tests/test_auth/group_token.py.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

USERNAME = "gtoi_visitor"
GROUP_NAME = "gtoi_group"
PWORD = "gtoi##mojo99"


def gt(token):
    """Authorization header for a group token."""
    return {"Authorization": f"grouptoken {token}"}


@th.django_unit_setup()
def setup_scheme_opt_in(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.services import group_token

    # Long-lived DB: delete before creating.
    User.objects.filter(username=USERNAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    group = Group.objects.create(name=GROUP_NAME, kind="organization")
    visitor = User(username=USERNAME, email=f"{USERNAME}@example.com",
                   display_name="Opt-in Visitor")
    visitor.save()
    visitor.is_email_verified = True
    visitor.save_password(PWORD)
    group.add_member(visitor)
    opts.token_a = group_token.mint(visitor, group)


@th.django_unit_test("the grouptoken scheme is opt-in per deployment")
def test_scheme_is_opt_in(opts):
    with th.server_settings(AUTH_BEARER_HANDLERS={}):
        resp = opts.client.get("/api/user/me", headers=gt(opts.token_a))
        assert_eq(resp.status_code, 401,
                  f"with the handler unregistered the scheme must be rejected, "
                  f"got {resp.status_code}: {resp.response}")
        assert_true("Invalid token type" in str(resp.response.get("error", "")),
                    f"an unregistered scheme reports 'Invalid token type', "
                    f"got {resp.response}")

    resp = opts.client.get("/api/user/me", headers=gt(opts.token_a))
    assert_eq(resp.status_code, 200,
              f"with the handler registered again the token must authenticate, "
              f"got {resp.status_code}: {resp.response}")
