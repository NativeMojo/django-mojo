"""
Tests for POST /api/assistant/context — creating conversations pre-loaded
with context from any MojoModel instance.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_EMAIL_ADMIN = 'ctx-admin@example.com'
TEST_EMAIL_LIMITED = 'ctx-limited@example.com'
TEST_EMAIL_NOAUTH = 'ctx-noauth@example.com'
TEST_EMAIL_MODEL_AUTHOR = 'ctx-model-author@example.com'
TEST_PASSWORD = 'TestPass1!'


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.incident")
def setup_context(opts):
    from mojo.apps.account.models import ApiKey, Group, User
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.incident.models import (
        Incident, MojoSecPolicyEvaluation, MojoSecPolicyProposal,
        Ticket, TicketNote)
    from mojo.apps.incident.models.mojosec_immutable import canonical_digest

    # Clean up prior mutable test data. The dedicated model author is retained
    # because immutable proposal/evaluation audit rows protect their author FK.
    Group.objects.filter(name="[CTX-TEST] Assistant Context").delete()
    User.objects.filter(email__in=[TEST_EMAIL_ADMIN, TEST_EMAIL_LIMITED, TEST_EMAIL_NOAUTH]).delete()
    model_author, _ = User.objects.get_or_create(
        username=TEST_EMAIL_MODEL_AUTHOR,
        defaults={"email": TEST_EMAIL_MODEL_AUTHOR})

    opts.admin = User.objects.create_user(
        username=TEST_EMAIL_ADMIN, email=TEST_EMAIL_ADMIN, password=TEST_PASSWORD,
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_security")
    opts.admin.add_permission("manage_security")
    opts.admin.add_permission("manage_groups")

    # User with view_admin but NOT view_security
    opts.limited = User.objects.create_user(
        username=TEST_EMAIL_LIMITED, email=TEST_EMAIL_LIMITED, password=TEST_PASSWORD,
    )
    opts.limited.is_email_verified = True
    opts.limited.save()
    opts.limited.add_permission("view_admin")

    opts.noauth = User.objects.create_user(
        username=TEST_EMAIL_NOAUTH, email=TEST_EMAIL_NOAUTH, password=TEST_PASSWORD,
    )
    opts.noauth.is_email_verified = True
    opts.noauth.save()

    # Clean up stale test data
    Ticket.objects.filter(title__startswith="[CTX-TEST]").delete()
    Incident.objects.filter(title__startswith="[CTX-TEST]").delete()
    Conversation.objects.filter(user__in=[opts.admin, opts.limited, opts.noauth]).delete()

    # Create a ticket with notes
    opts.ticket = Ticket.objects.create(
        title="[CTX-TEST] Suspicious login pattern",
        description="Multiple failed logins from 10.0.0.1",
        status="open",
        priority=7,
        category="security",
        user=opts.admin,
    )
    TicketNote.objects.create(parent=opts.ticket, note="First note", user=opts.admin)
    TicketNote.objects.create(parent=opts.ticket, note="Second note", user=opts.admin)

    # Create an incident with history
    opts.incident = Incident.objects.create(
        title="[CTX-TEST] SSH brute force",
        details="Repeated failed SSH from 10.0.0.1",
        status="investigating",
        priority=8,
        category="ossec:auth",
        source_ip="10.0.0.1",
        hostname="web-prod-01",
    )
    opts.incident.add_history("created", note="Incident created by RuleSet")
    opts.incident.add_history("handler:block", note="IP 10.0.0.1 blocked for 3600s")

    opts.context_group = Group.objects.create(
        name="[CTX-TEST] Assistant Context", kind="organization")
    opts.context_group.add_member(opts.admin)
    opts.context_api_key, _ = ApiKey.create_for_group(
        opts.context_group, "[CTX-TEST] denied credential")
    proposal_content = {
        "schema": "mojosec.policy-proposal.v1",
        "detectors": [{
            "kind": "web.probe", "decision": "flag", "minimum_count": 2,
        }],
    }
    opts.context_proposal = MojoSecPolicyProposal.objects.create(
        created_by=model_author, summary="[CTX-TEST] denied proposal",
        content=proposal_content, content_digest=canonical_digest(proposal_content))
    opts.context_evaluation = MojoSecPolicyEvaluation.objects.create(
        proposal=opts.context_proposal, created_by=model_author,
        mode=MojoSecPolicyEvaluation.REPLAY, sample_count=0,
        sample_digest="1" * 64, result_digest="2" * 64,
        metrics={"evaluated": 0})


# ---------------------------------------------------------------------------
# Ticket context
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_ticket(opts):
    """Create conversation from ticket — context includes title, description, notes."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": opts.ticket.pk},
    )
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.status, f"Expected success, got: {resp.json}")
    conv_id = resp.json.data.conversation_id
    assert_true(conv_id, "Expected a conversation_id")
    opts.ticket_conv_id = conv_id

    # Verify conversation
    from mojo.apps.assistant.models import Conversation, Message
    conv = Conversation.objects.get(pk=conv_id)
    assert_eq(conv.user_id, opts.admin.pk, "Conversation should be owned by admin")
    assert_true("Ticket #" in conv.title, f"Title should contain 'Ticket #', got: {conv.title}")

    # Verify metadata
    assert_eq(conv.metadata.get("source_model"), "incident.ticket",
              f"Expected source_model='incident.ticket', got: {conv.metadata}")
    assert_eq(conv.metadata.get("source_pk"), opts.ticket.pk,
              f"Expected source_pk={opts.ticket.pk}, got: {conv.metadata}")

    # Verify context message
    msgs = Message.objects.filter(conversation=conv)
    assert_eq(msgs.count(), 1, f"Expected 1 context message, got {msgs.count()}")
    msg = msgs.first()
    assert_eq(msg.role, "user", f"Context message role should be 'user', got: {msg.role}")
    assert_true("Suspicious login" in msg.content,
                f"Context should include ticket title, got: {msg.content[:200]}")
    assert_true("First note" in msg.content,
                f"Context should include notes, got: {msg.content[:500]}")


# ---------------------------------------------------------------------------
# Incident context
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_incident(opts):
    """Create conversation from incident — context includes details, history."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Incident", "pk": opts.incident.pk},
    )
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.status, f"Expected success, got: {resp.json}")
    conv_id = resp.json.data.conversation_id
    opts.incident_conv_id = conv_id

    from mojo.apps.assistant.models import Conversation, Message
    conv = Conversation.objects.get(pk=conv_id)
    assert_true("Incident #" in conv.title, f"Title should contain 'Incident #', got: {conv.title}")
    assert_eq(conv.metadata.get("source_model"), "incident.incident",
              f"Expected source_model='incident.incident', got: {conv.metadata}")

    msg = Message.objects.filter(conversation=conv).first()
    assert_true("SSH brute force" in msg.content,
                f"Context should include incident title, got: {msg.content[:200]}")
    assert_true("10.0.0.1" in msg.content,
                f"Context should include source IP, got: {msg.content[:500]}")
    assert_true("History" in msg.content,
                f"Context should include history section, got: {msg.content[:500]}")


# ---------------------------------------------------------------------------
# Duplicate prevention
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_duplicate_returns_existing(opts):
    """Same user + same model + same pk returns existing conversation."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": opts.ticket.pk},
    )
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.status, f"Expected success, got: {resp.json}")
    conv_id = resp.json.data.conversation_id
    assert_eq(conv_id, opts.ticket_conv_id,
              f"Expected existing conv {opts.ticket_conv_id}, got new {conv_id}")
    assert_true(resp.json.data.get("existing"),
                "Expected existing=True flag on duplicate")


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_permission_denied_no_admin(opts):
    """User without view_admin gets denied."""
    opts.client.login(TEST_EMAIL_NOAUTH, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": opts.ticket.pk},
    )
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")


@th.django_unit_test()
def test_context_model_permission_denied(opts):
    """User with view_admin but without model VIEW_PERMS gets denied."""
    opts.client.login(TEST_EMAIL_LIMITED, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": opts.ticket.pk},
    )
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")
    assert_true("Permission denied" in str(resp.json),
                f"Expected permission denied error, got: {resp.json}")


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_invalid_model(opts):
    """Invalid model string returns 400."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "not.AModel", "pk": 1},
    )
    assert_eq(resp.status_code, 400, f"Expected 400, got {resp.status_code}")


@th.django_unit_test()
def test_context_missing_instance(opts):
    """Nonexistent pk returns 404."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": 999999},
    )
    assert_eq(resp.status_code, 404, f"Expected 404, got {resp.status_code}")


@th.django_unit_test()
def test_context_bad_model_format(opts):
    """Model string without dot returns 400."""
    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "Ticket", "pk": 1},
    )
    assert_eq(resp.status_code, 400, f"Expected 400, got {resp.status_code}")


@th.django_unit_test()
def test_context_deny_ai_models_fail_before_conversation_mutation(opts):
    """DENY_AI blocks context attachment even with model permissions and a group."""
    from mojo.apps.assistant.models import Conversation, Message

    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    before_conversations = Conversation.objects.filter(user=opts.admin).count()
    before_messages = Message.objects.filter(conversation__user=opts.admin).count()
    denied = (
        ("incident.MojoSecPolicyProposal", opts.context_proposal.pk),
        ("incident.MojoSecPolicyEvaluation", opts.context_evaluation.pk),
        ("account.ApiKey", opts.context_api_key.pk),
    )
    for model_string, pk in denied:
        resp = opts.client.post(
            "/api/assistant/context",
            {"model": model_string, "pk": pk, "group": opts.context_group.pk},
        )
        assert_eq(
            resp.status_code, 403,
            f"DENY_AI context for {model_string} must return 403: {resp.json}")
        assert_true(
            "not available to the assistant" in str(resp.json),
            f"DENY_AI must win over model/group permissions for {model_string}: {resp.json}")

    assert_eq(
        Conversation.objects.filter(user=opts.admin).count(), before_conversations,
        "denied context attempts must not create or tenant-stamp a conversation")
    assert_eq(
        Message.objects.filter(conversation__user=opts.admin).count(), before_messages,
        "denied context attempts must not create a serialized context message")
    assert_true(not Conversation.objects.filter(
        user=opts.admin, group=opts.context_group,
        metadata__source_model__in=[item[0].lower() for item in denied]).exists(),
        "request.group must not become a tenant stamp on a DENY_AI attempt")


# ---------------------------------------------------------------------------
# Generic model context (non-ticket, non-incident)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_context_generic_model(opts):
    """A model without a rich builder still gets generic context."""
    from mojo.apps.incident.models import RuleSet

    rs = RuleSet.objects.create(
        name="[CTX-TEST] Test Rule",
        category="test",
        is_active=False,
    )

    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.RuleSet", "pk": rs.pk, "group": opts.context_group.pk},
    )
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.status, f"Expected success, got: {resp.json}")
    conv_id = resp.json.data.conversation_id

    from mojo.apps.assistant.models import Conversation, Message
    conv = Conversation.objects.get(pk=conv_id)
    assert_true("RuleSet" in conv.title, f"Title should contain 'RuleSet', got: {conv.title}")
    assert_eq(conv.group_id, opts.context_group.pk,
              "an allowed model must preserve the existing request.group stamp")

    msg = Message.objects.filter(conversation=conv).first()
    assert_true(msg.content, "Context message should have content")

    # Cleanup
    rs.delete()
