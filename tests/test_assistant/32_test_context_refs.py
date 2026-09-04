"""Tests for the add_context assistant tool and context reference handling."""
from testit import helpers as th


TEST_ADMIN_EMAIL = "ctxref_admin@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_context_refs(opts):
    from mojo.apps.account.models import User
    from mojo.apps.assistant.models import Skill

    Skill.objects.filter(name__startswith="ctxref_").delete()
    User.objects.filter(email=TEST_ADMIN_EMAIL).delete()
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN_EMAIL, email=TEST_ADMIN_EMAIL, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    for perm in ["view_admin", "view_security", "security"]:
        opts.admin.add_permission(perm)

    # Seed ordinary AI-visible models. Governed security models such as
    # RuleSet deliberately reject assistant references.
    opts.skill = Skill.objects.create(
        user=opts.admin,
        tier="user",
        name="ctxref_test_skill",
        steps=[],
    )
    opts.other_skill = Skill.objects.create(
        user=opts.admin,
        tier="user",
        name="ctxref_other_skill",
        steps=[],
    )


def _add_context(params, user):
    from mojo.apps.assistant.services.tools.models import _tool_add_context
    return _tool_add_context(params, user)


# ---------------------------------------------------------------------------
# Valid references pass through
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_valid_ref_passes(opts):
    result = _add_context({
        "references": [{
            "app_name": "assistant",
            "model_name": "Skill",
            "pk": opts.skill.pk,
            "label": "Test skill",
        }],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 1, f"Expected 1 valid ref, got {len(refs)}"
    assert refs[0]["pk"] == opts.skill.pk, "Should return the correct pk"
    assert refs[0]["label"] == "Test skill", "Should preserve label"


@th.django_unit_test()
def test_multiple_valid_refs(opts):
    result = _add_context({
        "references": [
            {"app_name": "assistant", "model_name": "Skill", "pk": opts.skill.pk, "label": "Skill"},
            {"app_name": "assistant", "model_name": "Skill", "pk": opts.other_skill.pk, "label": "Other"},
        ],
    }, opts.admin)
    assert "error" not in result, f"Should succeed: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 2, f"Expected 2 valid refs, got {len(refs)}"


# ---------------------------------------------------------------------------
# Invalid model name filtered
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_invalid_model_filtered(opts):
    result = _add_context({
        "references": [{
            "app_name": "incident",
            "model_name": "NonExistentModel",
            "pk": 1,
            "label": "Bad",
        }],
    }, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 0, f"Invalid model should be filtered, got {len(refs)} refs"


@th.django_unit_test()
def test_invalid_app_filtered(opts):
    result = _add_context({
        "references": [{
            "app_name": "nonexistent_app",
            "model_name": "Skill",
            "pk": 1,
            "label": "Bad",
        }],
    }, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 0, f"Invalid app should be filtered, got {len(refs)} refs"


# ---------------------------------------------------------------------------
# Non-existent pk filtered
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_nonexistent_pk_filtered(opts):
    result = _add_context({
        "references": [{
            "app_name": "assistant",
            "model_name": "Skill",
            "pk": 999999999,
            "label": "Ghost",
        }],
    }, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 0, f"Non-existent pk should be filtered, got {len(refs)} refs"


# ---------------------------------------------------------------------------
# Mixed valid/invalid refs
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_mixed_refs_only_valid_returned(opts):
    result = _add_context({
        "references": [
            {"app_name": "assistant", "model_name": "Skill", "pk": opts.skill.pk, "label": "Good"},
            {"app_name": "assistant", "model_name": "NonExistent", "pk": 1, "label": "Bad model"},
            {"app_name": "assistant", "model_name": "Skill", "pk": 999999999, "label": "Bad pk"},
            {"app_name": "assistant", "model_name": "Skill", "pk": opts.other_skill.pk, "label": "Good too"},
        ],
    }, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 2, f"Expected 2 valid refs from mixed input, got {len(refs)}"
    labels = [r["label"] for r in refs]
    assert "Good" in labels, f"Should have 'Good' ref, got {labels}"
    assert "Good too" in labels, f"Should have 'Good too' ref, got {labels}"


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_empty_references_returns_empty(opts):
    result = _add_context({"references": []}, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 0, f"Empty input should return empty, got {len(refs)} refs"


@th.django_unit_test()
def test_missing_required_fields_filtered(opts):
    result = _add_context({
        "references": [
            {"app_name": "assistant", "model_name": "Skill"},
            {"app_name": "assistant", "pk": 1},
            {"model_name": "Skill", "pk": 1},
        ],
    }, opts.admin)
    assert "error" not in result, f"Should not error: {result.get('error')}"
    refs = result["references"]
    assert len(refs) == 0, f"Refs missing required fields should be filtered, got {len(refs)}"


# ---------------------------------------------------------------------------
# Context block validation (agent layer)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_block_validation_valid(opts):
    from mojo.apps.assistant.services.agent import _validate_block
    block = {"type": "context", "references": [{"app_name": "assistant", "model_name": "Skill", "pk": 1}]}
    assert _validate_block(block) is True, "Valid context block should pass validation"


@th.django_unit_test()
def test_context_block_validation_empty_refs(opts):
    from mojo.apps.assistant.services.agent import _validate_block
    block = {"type": "context", "references": []}
    assert _validate_block(block) is False, "Context block with empty refs should fail validation"


@th.django_unit_test()
def test_context_block_validation_no_refs_key(opts):
    from mojo.apps.assistant.services.agent import _validate_block
    block = {"type": "context"}
    assert _validate_block(block) is False, "Context block without references key should fail validation"


# ---------------------------------------------------------------------------
# _extract_context_refs helper
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_extract_context_refs(opts):
    import ujson
    from mojo.apps.assistant.services.agent import _extract_context_refs

    tool_blocks = [
        {"id": "call_1", "name": "query_model", "input": {}},
        {"id": "call_2", "name": "add_context", "input": {}},
    ]
    tool_results = [
        {"tool_use_id": "call_1", "content": ujson.dumps({"results": []})},
        {"tool_use_id": "call_2", "content": ujson.dumps({
            "references": [{"app_name": "assistant", "model_name": "Skill", "pk": 1, "label": "Test"}],
        })},
    ]
    refs = _extract_context_refs(tool_blocks, tool_results)
    assert len(refs) == 1, f"Should extract 1 ref, got {len(refs)}"
    assert refs[0]["label"] == "Test", f"Should extract correct label, got {refs[0]}"


@th.django_unit_test()
def test_extract_context_refs_no_add_context(opts):
    import ujson
    from mojo.apps.assistant.services.agent import _extract_context_refs

    tool_blocks = [{"id": "call_1", "name": "query_model", "input": {}}]
    tool_results = [{"tool_use_id": "call_1", "content": ujson.dumps({"results": []})}]
    refs = _extract_context_refs(tool_blocks, tool_results)
    assert len(refs) == 0, f"Should return empty when no add_context call, got {len(refs)}"
