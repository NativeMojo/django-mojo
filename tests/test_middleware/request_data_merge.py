"""
Regression tests for ITEM-024 — the same key sent in both the query string and
the JSON body must resolve deterministically (later source wins: JSON body over
query params), not merge into a list that crashes the dispatcher into a bare
Django 500.

Before the fix, RequestDataParser._set_nested_value merged a cross-source
duplicate into a mixed list (['518', 518]); the dispatcher's
int(request.DATA.group) then raised TypeError, which the surrounding
`except ValueError` did not catch, and the block runs before
dispatch_error_handler wraps the view — so Django returned its stock 500 HTML
page with nothing in mojo's logs.

Parser tests build fake requests in-process (the parser only touches method,
content_type, GET, POST, FILES, body); HTTP tests drive the real middleware +
dispatcher chain through the test server.
"""
from testit import helpers as th
from objict import objict


USERNAME = "dup_key_merge@test.com"
PASSWORD = "dup_key_merge_pw_99"
GROUP_NAME = "dup-key-merge-group"


def _fake_request(query="", body=None, content_type="application/json",
                  method="POST", form=""):
    """Build the minimal request surface RequestDataParser touches.

    Imports live inside the helper so Django is configured before QueryDict is
    pulled in (testit convention)."""
    from django.http import QueryDict
    req = objict()
    req.method = method
    req.content_type = content_type
    req.GET = QueryDict(query)
    req.POST = QueryDict(form)
    req.FILES = QueryDict()
    req.body = body if body is not None else b""
    return req


@th.django_unit_setup()
def setup_dup_key_merge(opts):
    from mojo.apps.account.models import User, Group

    User.objects.filter(email=USERNAME).delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    user = User.objects.create_user(username=USERNAME, email=USERNAME, password=PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    opts.user_id = user.pk

    grp = Group.objects.create(name=GROUP_NAME, kind="organization")
    opts.group_id = grp.pk


@th.django_unit_test("parser: JSON body wins over query string for the same key (THE regression)")
def test_parser_json_body_wins_over_query(opts):
    from mojo.helpers.request_parser import parse_request_data

    req = _fake_request(query="group=518", body=b'{"group": 518}')
    data = parse_request_data(req)

    assert not isinstance(data.group, list), \
        f"a key duplicated across query string and JSON body must not merge into a list, got {data.group!r}"
    assert data.group == 518, \
        f"JSON body must win over the query string for a duplicated key, got {data.group!r}"


@th.django_unit_test("parser: form body wins over query string for the same key")
def test_parser_form_wins_over_query(opts):
    from mojo.helpers.request_parser import parse_request_data

    req = _fake_request(query="status=a", form="status=b",
                        content_type="application/x-www-form-urlencoded")
    data = parse_request_data(req)

    assert data.status == "b", \
        f"form body must win over the query string for a duplicated key, got {data.status!r}"


@th.django_unit_test("parser: repeated query keys and array notation still produce lists")
def test_parser_multivalue_query_preserved(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(query="tag=a&tag=b", method="GET"))
    assert data.tag == ["a", "b"], \
        f"repeated query key ?tag=a&tag=b must still produce a list, got {data.tag!r}"

    data = parse_request_data(_fake_request(query="tags[]=x&tags[]=y", method="GET"))
    assert data.tags == ["x", "y"], \
        f"array notation tags[]=x&tags[]=y must still produce a list, got {data.tags!r}"


@th.django_unit_test("parser: a JSON list value replaces a query scalar, intact")
def test_parser_json_list_replaces_query_scalar(opts):
    from mojo.helpers.request_parser import parse_request_data

    req = _fake_request(query="ids=5", body=b'{"ids": [1, 2]}')
    data = parse_request_data(req)

    assert data.ids == [1, 2], \
        f"a JSON list must replace the query scalar whole (not prepend to it), got {data.ids!r}"


@th.django_unit_test("parser: nested dict from JSON replaces the query-built dict whole")
def test_parser_nested_whole_value_replace(opts):
    from mojo.helpers.request_parser import parse_request_data

    req = _fake_request(query="user.name=John", body=b'{"user": {"name": "Jane"}}')
    data = parse_request_data(req)

    # Whole-value replace: JSON's dict wins outright (no deep-merge, no list).
    # Compared as a dict — nested dicts from a JSON body have always been
    # stored as plain dicts (pre-existing; unrelated to the collision fix).
    assert data.user == {"name": "Jane"}, \
        f"JSON's nested dict must replace the query-built dict whole, got {data.user!r}"


@th.django_unit_test("parser: single-source values are unchanged by the fix")
def test_parser_single_source_unchanged(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(query="group=518", method="GET"))
    assert data.group == "518", \
        f"query-only value must stay a plain string, got {data.group!r}"

    data = parse_request_data(_fake_request(body=b'{"group": 518}'))
    assert data.group == 518, \
        f"JSON-only value must arrive intact as an int, got {data.group!r}"


@th.django_unit_test("HTTP: same group key in query string AND JSON body returns 200, not a bare 500")
def test_http_duplicate_group_key_returns_200(opts):
    assert opts.client.login(USERNAME, PASSWORD), "self login failed"

    # Control — key in only one place (body) is the known-good baseline.
    resp = opts.client.post(
        f"/api/user/{opts.user_id}",
        {"group": opts.group_id, "display_name": "Dup Control"},
    )
    assert resp.status_code == 200, \
        f"control (group in body only) should succeed, got {resp.status_code}: {opts.client.last_response.body}"

    # Regression — same key in BOTH channels crashed the dispatcher (bare 500).
    resp = opts.client.post(
        f"/api/user/{opts.user_id}?group={opts.group_id}",
        {"group": opts.group_id, "display_name": "Dup Both"},
    )
    opts.client.logout()
    assert resp.status_code == 200, \
        f"the same group key in query string AND JSON body must not 500, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("HTTP: a genuinely unusable group value returns mojo's JSON 400, not a bare 500")
def test_http_unusable_group_returns_400(opts):
    assert opts.client.login(USERNAME, PASSWORD), "self login failed"

    # A dict can never coerce to a group id — int(dict) raises TypeError, which
    # previously escaped the dispatcher's `except ValueError` as a bare 500.
    resp = opts.client.post(
        f"/api/user/{opts.user_id}?group={opts.group_id}",
        {"group": {"bad": True}},
    )
    opts.client.logout()
    assert resp.status_code == 400, \
        f"an uncoercible group value must return a clean 400, got {resp.status_code}: {opts.client.last_response.body}"
    body = str(opts.client.last_response.body)
    assert "Invalid group ID" in body, \
        f"the 400 must be mojo's JSON 'Invalid group ID' envelope, got: {body}"


@th.django_unit_test("requires_perms: unusable group param fails closed to PermissionDenied, not a coercion crash")
def test_requires_perms_unusable_group_fails_closed(opts):
    import mojo.errors
    from mojo.decorators.auth import requires_perms

    @requires_perms("item024_nonexistent_perm")
    def dummy_view(request):
        return "must never be reached"

    fake_user = objict(is_authenticated=True, username="item024-fake",
                       has_permission=lambda perms: False)
    # group='' is falsy, so the dispatcher skips resolution and the decorator's
    # own int('') coercion runs — previously an unhandled ValueError (JSON 500).
    req = objict(user=fake_user, DATA=objict(group=""), group=None)

    try:
        result = dummy_view(req)
        assert False, \
            f"view must not execute for a permissionless user with an unusable group param, returned {result!r}"
    except mojo.errors.PermissionDeniedException:
        pass  # fail-closed deny is the correct outcome
