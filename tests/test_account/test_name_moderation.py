"""Regression: display-name moderation flags, it does not hard-block (ITEM-007).

content_guard matches profanity as naive substrings, so legitimate names that
merely CONTAIN a high-severity substring — Matsushita ("shit"), Scunthorpe
("cunt"), common South-Asian names like Harshita ("shit") — used to fail
registration with "Invalid display name: contains inappropriate content".
`User.validate_name_fields` now logs/flags such names and ALLOWS them;
content_guard's own scoring is unchanged.
"""
from testit import helpers as th


# Legitimate names that contain a high-severity profanity substring and were
# hard-blocked at signup before the fix.
_FALSE_POSITIVE_NAMES = ["Matsushita", "Harshita", "Scunthorpe"]


@th.django_unit_test("display-name moderation flags but does NOT block legit names with a profanity substring")
def test_name_fields_flag_not_block(opts):
    from mojo.apps.account.models import User
    from mojo import errors as merrors

    for name in _FALSE_POSITIVE_NAMES:
        user = User(display_name=name)
        try:
            user.validate_name_fields(changed_fields=["display_name"], created=True)
        except merrors.ValueException as exc:
            assert False, (
                f"{name!r} is a legitimate name and must be allowed (flagged, not "
                f"blocked) at signup, but validate_name_fields raised: {exc}")


@th.django_unit_test("display-name moderation still allows an ordinary clean name")
def test_name_fields_allow_clean_name(opts):
    from mojo.apps.account.models import User
    from mojo import errors as merrors

    user = User(display_name="Jane Smith")
    try:
        user.validate_name_fields(changed_fields=["display_name"], created=True)
    except merrors.ValueException as exc:
        assert False, f"a clean name must pass validation, but it raised: {exc}"


@th.django_unit_test("content_guard still scores these names as block (guard unchanged; only the caller's response changed)")
def test_content_guard_scoring_unchanged(opts):
    from mojo.helpers import content_guard

    result = content_guard.check_text(
        "Matsushita", surface="name", policy={"text_block_threshold": 50})
    assert result.decision == "block", (
        "content_guard's scoring must be UNCHANGED — only validate_name_fields' "
        f"response changed. Expected 'Matsushita' to still score block, got {result.decision}")
