"""Moved from the default-tier sibling (maestro item #1839): these tests mutate shared testit/production module state process-wide (seam rebinding, module-attribute save/restore), which races every parallel module.
"""
"""Tests for the add_context assistant tool and context reference handling."""
from testit import helpers as th


TEST_ADMIN_EMAIL = "ctxref_admin@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_context_refs(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event, RuleSet

    User.objects.filter(email=TEST_ADMIN_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "security"]:
        opts.admin.add_permission(perm)

    # Seed data
    RuleSet.objects.filter(name__startswith="ctxref_").delete()
    Event.objects.filter(title__startswith="ctxref_").delete()

    opts.ruleset = RuleSet.objects.create(
        name="ctxref_test_rule", category="ctxref_cat",
    )
    opts.event = Event.objects.create(
        title="ctxref_test_event",
        details="seed event",
        category="ctxref_cat",
        level=3,
        scope="global",
    )


def _add_context(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_add_context
    return _tool_add_context(params, user)


# ---------------------------------------------------------------------------
# Valid references pass through
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Invalid model name filtered
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Non-existent pk filtered
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# DENY_AI model filtered
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_deny_ai_model_filtered(opts):
    from mojo.apps.incident.models import RuleSet

    setattr(RuleSet.RestMeta, "DENY_AI_VIEW", True)
    try:
        result = _add_context({
            "references": [{
                "app_name": "incident",
                "model_name": "RuleSet",
                "pk": opts.ruleset.pk,
                "label": "Denied",
            }],
        }, opts.admin)
        assert "error" not in result, f"Should not error: {result.get('error')}"
        refs = result["references"]
        assert len(refs) == 0, f"DENY_AI_VIEW model should be filtered, got {len(refs)} refs"
    finally:
        delattr(RuleSet.RestMeta, "DENY_AI_VIEW")


# ---------------------------------------------------------------------------
# Mixed valid/invalid refs
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Context block validation (agent layer)
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# _extract_context_refs helper
# ---------------------------------------------------------------------------



