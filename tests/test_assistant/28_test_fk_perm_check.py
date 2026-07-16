"""Tests for FK-by-pk permission gate in MojoModel.on_rest_save_related_field.

Verifies that scalar-pk FK assignments respect the related model's VIEW_PERMS,
silently skipping the assignment on denial (matching the dict-value branch).
"""
from testit import helpers as th


TEST_PRIV_EMAIL = "fkperm_priv@test.com"
TEST_NOPRIV_EMAIL = "fkperm_nopriv@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_fk_perm(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.assistant.models import Skill

    # Clean prior test data
    User.objects.filter(email__in=[TEST_PRIV_EMAIL, TEST_NOPRIV_EMAIL]).delete()
    Skill.objects.filter(name__startswith="fkperm_").delete()
    Group.objects.filter(name__startswith="fkperm_").delete()

    # User with both view_admin AND group view perms
    opts.priv = User.objects.create_user(
        username=TEST_PRIV_EMAIL, email=TEST_PRIV_EMAIL, password="pass123",
    )
    opts.priv.is_email_verified = True
    opts.priv.save()
    for perm in ["view_admin", "view_groups"]:
        opts.priv.add_permission(perm)

    # User with view_admin only — can save Skills, cannot view Groups
    opts.nopriv = User.objects.create_user(
        username=TEST_NOPRIV_EMAIL, email=TEST_NOPRIV_EMAIL, password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    opts.group = Group.objects.create(name="fkperm_group", kind="generic")


def _build_synthetic_request(user):
    """Build the same shape of synthetic request the assistant tools use."""
    import objict
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = None
    req.bearer = None
    req.ip = "test"
    req.path = "/test/fk_perm"
    req.META = {}
    req.api_key = None
    return req


# ---------------------------------------------------------------------------
# Scalar-pk: gated by VIEW_PERMS on the related model
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_fk_assign_succeeds_with_view_perms(opts):
    """User with VIEW_PERMS on related model can set FK by pk."""
    from mojo.apps.assistant.models import Skill

    skill = Skill(tier="user", name="fkperm_priv_assign")
    request = _build_synthetic_request(opts.priv)
    skill.on_rest_save(request, {"name": "fkperm_priv_assign", "tier": "user", "group": opts.group.pk})

    skill.refresh_from_db()
    assert skill.group_id == opts.group.pk, \
        f"FK should be assigned to group {opts.group.pk}, got {skill.group_id}"

    skill.delete()


@th.django_unit_test()
def test_fk_assign_silently_skipped_without_view_perms(opts):
    """User without VIEW_PERMS on related model: assignment silently no-ops, parent save still succeeds."""
    from mojo.apps.assistant.models import Skill

    skill = Skill(tier="user", name="fkperm_nopriv_assign")
    request = _build_synthetic_request(opts.nopriv)
    skill.on_rest_save(request, {"name": "fkperm_nopriv_assign", "tier": "user", "group": opts.group.pk})

    skill.refresh_from_db()
    assert skill.group_id is None, \
        f"FK should be silently skipped (None) for user without VIEW_PERMS, got group_id={skill.group_id}"
    # Parent save proceeded — the Skill exists with the other fields applied
    assert skill.name == "fkperm_nopriv_assign", \
        f"Parent save should still apply other fields, got name={skill.name!r}"

    skill.delete()


@th.django_unit_test()
def test_fk_assign_string_pk_also_gated(opts):
    """Scalar-pk gate applies whether pk is int or string-form int."""
    from mojo.apps.assistant.models import Skill

    skill = Skill(tier="user", name="fkperm_str_pk")
    request = _build_synthetic_request(opts.nopriv)
    skill.on_rest_save(request, {
        "name": "fkperm_str_pk", "tier": "user", "group": str(opts.group.pk),
    })

    skill.refresh_from_db()
    assert skill.group_id is None, \
        f"String-form pk should also be gated, got group_id={skill.group_id}"

    skill.delete()


@th.django_unit_test()
def test_fk_clear_to_none_skips_perm_check(opts):
    """Clearing an FK (value=0/None/'') is allowed without VIEW_PERMS — no target to view."""
    from mojo.apps.assistant.models import Skill

    # Pre-seed a skill whose FK is set
    skill = Skill.objects.create(
        tier="user", name="fkperm_clear", user=opts.nopriv, group=opts.group,
    )

    request = _build_synthetic_request(opts.nopriv)
    skill.on_rest_save(request, {"group": 0})

    skill.refresh_from_db()
    assert skill.group_id is None, \
        f"FK clear (group=0) should always succeed, got group_id={skill.group_id}"

    skill.delete()


# ---------------------------------------------------------------------------
# Dict-value path is unchanged
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_dict_path_unchanged(opts):
    """Passing dict (not pk) still hits the dict-value branch and its existing perm check."""
    from mojo.apps.assistant.models import Skill

    # Pre-seed with the group already set so the dict-path's "update existing" semantics apply
    skill = Skill.objects.create(
        tier="user", name="fkperm_dict_path", user=opts.priv, group=opts.group,
    )

    request = _build_synthetic_request(opts.priv)
    # Dict value triggers the related_instance.on_rest_save path (line 1080).
    # We just verify it doesn't crash and the FK stays attached — no behavior change.
    skill.on_rest_save(request, {"group": {"name": "fkperm_dict_path_renamed"}})

    skill.refresh_from_db()
    assert skill.group_id == opts.group.pk, \
        f"Dict path should preserve FK, got group_id={skill.group_id}"

    skill.delete()


# ---------------------------------------------------------------------------
# Incident reporting on denial
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_denied_assignment_records_incident(opts):
    """Silent skip still records an incident event via rest_check_permission."""
    from mojo.apps.assistant.models import Skill
    from mojo.apps.incident.models import Event

    # Scope to this test's user — a global count races with other modules
    # (test_models/fk_attach_audit.py deletes fk_attach_denied events in its
    # setup) when the suite runs in parallel.
    before = Event.objects.filter(category="fk_attach_denied", uid=opts.nopriv.id).count()

    skill = Skill(tier="user", name="fkperm_incident")
    request = _build_synthetic_request(opts.nopriv)
    skill.on_rest_save(request, {"name": "fkperm_incident", "tier": "user", "group": opts.group.pk})

    after = Event.objects.filter(category="fk_attach_denied", uid=opts.nopriv.id).count()
    assert after > before, \
        f"Silent FK denial should still report an fk_attach_denied incident event, before={before} after={after}"

    skill.refresh_from_db()
    skill.delete()
