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
TEST_EMAIL_MEMBER = 'ctx-member@example.com'
TEST_EMAIL_TENANT = 'ctx-tenant@example.com'
TEST_PASSWORD = 'TestPass1!'
NOT_FOUND = "Context source not found"
GROUP_MARKER = "CTX-GROUP-SETTINGS-MARKER"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
@th.requires_app("mojo.apps.incident")
def setup_context(opts):
    from mojo.apps.account.models import ApiKey, Group, Notification, User
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.incident.models import (
        Incident, MojoSecPolicyEvaluation, MojoSecPolicyProposal,
        Ticket, TicketNote)
    from mojo.apps.incident.models.mojosec_immutable import canonical_digest

    # Clean up prior mutable test data. The dedicated model author is retained
    # because immutable proposal/evaluation audit rows protect their author FK.
    Group.objects.filter(name__startswith="[CTX-TEST]").delete()
    User.objects.filter(email__in=[
        TEST_EMAIL_ADMIN, TEST_EMAIL_LIMITED, TEST_EMAIL_NOAUTH,
        TEST_EMAIL_MEMBER, TEST_EMAIL_TENANT]).delete()
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

    # Who may read one row (#1553). Both users hold the global view_admin the
    # endpoint asks for and nothing else global.
    def user(email):
        account = User.objects.create_user(username=email, email=email, password=TEST_PASSWORD)
        account.is_email_verified = True
        account.save()
        account.add_permission("view_admin")
        return account

    opts.member = user(TEST_EMAIL_MEMBER)
    opts.tenant_viewer = user(TEST_EMAIL_TENANT)
    opts.source_group = Group.objects.create(
        name="[CTX-TEST] Source Tenant", kind="organization",
        metadata={"ctx_marker": GROUP_MARKER})
    opts.other_group = Group.objects.create(name="[CTX-TEST] Other Tenant", kind="organization")
    # a plain member: may see the group, in its `basic` shape only
    opts.source_group.add_member(opts.member)
    # view_security in the source tenant only, by a member-level grant
    opts.source_group.add_member(opts.tenant_viewer).add_permission("view_security")
    opts.other_group.add_member(opts.tenant_viewer)

    def ticket(name, group=None):
        return Ticket.objects.create(
            title=f"[CTX-TEST] {name}", description="tenant ticket", status="open",
            priority=5, category="security", user=opts.admin, group=group)

    opts.source_ticket = ticket("Source tenant ticket", opts.source_group)
    opts.other_ticket = ticket("Other tenant ticket", opts.other_group)
    opts.groupless_ticket = ticket("Groupless ticket")
    Notification.objects.filter(title__startswith="[CTX-TEST]").delete()
    opts.notification = Notification.objects.create(
        user=opts.limited, title="[CTX-TEST] Owner-only row", body="for one person")

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
    """User with view_admin but without the row's VIEW_PERMS gets the same answer as a missing row."""
    opts.client.login(TEST_EMAIL_LIMITED, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "incident.Ticket", "pk": opts.ticket.pk},
    )
    assert_true(_is_not_found(resp),
                f"a row the caller may not read answers like a missing one, got "
                f"{resp.status_code}: {resp.json}")


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
    from mojo.apps.assistant.models import Skill

    skill = Skill.objects.create(
        user=opts.admin,
        tier="user",
        name="[CTX-TEST] Generic Skill",
        steps=[],
    )

    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context",
        {"model": "assistant.Skill", "pk": skill.pk, "group": opts.context_group.pk},
    )
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_true(resp.json.status, f"Expected success, got: {resp.json}")
    conv_id = resp.json.data.conversation_id

    from mojo.apps.assistant.models import Conversation, Message
    conv = Conversation.objects.get(pk=conv_id)
    assert_true("Skill" in conv.title, f"Title should contain 'Skill', got: {conv.title}")
    assert_eq(conv.group_id, None,
              "a source row with no group gives a conversation with no group, "
              "whatever group the caller sent")

    msg = Message.objects.filter(conversation=conv).first()
    assert_true(msg.content, "Context message should have content")

    # Cleanup
    skill.delete()


@th.tier("bug")
@th.django_unit_test()
def test_context_generic_model_uses_default_graph_not_detail(opts):
    """Generic context is built from the `ai`/`default` graph, never the wider `detail`."""
    from mojo.apps.assistant.models import Conversation, Message, Skill

    marker = "CTX-DETAIL-ONLY-MARKER"
    Skill.objects.filter(name="[CTX-TEST] Graph Skill").delete()
    skill = Skill.objects.create(
        user=opts.admin, tier="user", name="[CTX-TEST] Graph Skill",
        description="visible in default",
        triggers=[marker], steps=[{"note": marker}], metadata={"marker": marker},
    )
    assert_true(marker in str(skill.to_dict("detail")),
                "precondition: the detail graph carries the marker")

    opts.client.login(TEST_EMAIL_ADMIN, TEST_PASSWORD)
    resp = opts.client.post(
        "/api/assistant/context", {"model": "assistant.Skill", "pk": skill.pk})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    conv = Conversation.objects.get(pk=resp.json.data.conversation_id)
    content = Message.objects.filter(conversation=conv).first().content
    assert_true("visible in default" in content,
                f"Context should carry the default graph's fields, got: {content[:400]}")
    assert_true(marker not in content,
                "Generic context serialized through the wider `detail` graph")
    for key in ("triggers", "steps", "metadata"):
        assert_true(f"**{key}**" not in content, f"detail-only key '{key}' reached the context")
    assert_eq(conv.group_id, None, "serializing the row must not stamp a group on the conversation")

    skill.delete()


@th.tier("bug")
@th.django_unit_test()
def test_context_generic_title_comes_from_the_graph(opts):
    """The heading and conversation title carry a name only when the selected graph does."""
    from mojo.apps.assistant.models import Skill
    from mojo.apps.assistant.services.context import build_context

    private = "CTX-PRIVATE-NAME-MARKER"
    Skill.objects.filter(name=private).delete()
    skill = Skill.objects.create(
        user=opts.admin, tier="user", name=private, description="visible in ai", steps=[])

    # Additive: `default` and `detail` stay as shipped, and only the assistant's
    # own graph selection, called in-process here, reads `ai`.
    original = Skill.RestMeta
    graphs = dict(original.GRAPHS)
    graphs["ai"] = {"fields": ["id", "tier", "description"]}
    Skill.RestMeta = type("RestMeta", (original,), {"GRAPHS": graphs})
    try:
        title, message, error = build_context("assistant.Skill", skill)
    finally:
        Skill.RestMeta = original
    shown_title, shown_message, shown_error = build_context("assistant.Skill", skill)
    pk = skill.pk
    skill.delete()

    assert_eq(error, None, f"a graph without the name must still build a context, got: {error}")
    assert_true("visible in ai" in message,
                f"the context should carry the ai graph's fields, got: {message[:300]}")
    assert_true(private not in title,
                f"a name the graph leaves out reached the conversation title: {title}")
    assert_true(private not in message,
                f"a name the graph leaves out reached the context heading: {message[:300]}")
    assert_eq(title, f"Skill #{pk}",
              "with no name in the graph the title is the model and its id")

    assert_eq(shown_error, None, f"the default graph must build a context, got: {shown_error}")
    assert_true(private in shown_title and f"## {shown_title}" in shown_message,
                f"a name the graph does serialize still titles the context, got: {shown_title}")


@th.tier("bug")
@th.django_unit_test()
def test_context_generic_title_reads_serialized_data_only(opts):
    """`title` wins over `name`, and neither is used unless the graph serialized it."""
    from mojo.apps.assistant.services.context import _generic_title

    assert_eq(_generic_title("app.Thing", 7, {"id": 7, "public": "visible"}), "Thing #7",
              "with no title or name in the serialized row the title is the model and its id")
    assert_eq(_generic_title("app.Thing", 7, {"title": "A title", "name": "A name"}),
              "Thing #7: A title", "a serialized title heads the context")
    assert_eq(_generic_title("app.Thing", 7, {"title": "", "name": "A name"}),
              "Thing #7: A name", "an empty title falls back to the serialized name")
    assert_eq(_generic_title("app.Thing", 7, {"name": {"nested": "shape"}}), "Thing #7",
              "a name that is not text is not put in a heading")
    assert_eq(_generic_title("app.Thing", 7, {"title": "x" * 300}), "Thing #7: " + "x" * 100,
              "a long title is cut to 100 characters")


# ---------------------------------------------------------------------------
# Who may read the source row, and what the conversation is bound to (#1553)
# ---------------------------------------------------------------------------

def _post(opts, email, payload):
    opts.client.login(email, TEST_PASSWORD)
    try:
        return opts.client.post("/api/assistant/context", payload)
    finally:
        opts.client.logout()


def _is_not_found(resp):
    """The one answer for a missing row and for a row the caller may not read.

    `code` and `server` are added to every JSON response by the framework.
    """
    return (resp.status_code == 404 and resp.json is not None
            and resp.json.get("status") is False and resp.json.get("error") == NOT_FOUND
            and set(resp.json.keys()) <= {"status", "error", "code", "server"})


def _conversation(resp):
    from mojo.apps.assistant.models import Conversation
    return Conversation.objects.get(pk=resp.json.data.conversation_id)


def _content(conversation):
    from mojo.apps.assistant.models import Message
    return Message.objects.filter(conversation=conversation).first().content


def _source_request(user, group=None, **data):
    """A request as the endpoint sees it, for the service called in this process."""
    from mojo.apps.assistant.services.tools.models import _build_request
    request = _build_request(user, filters=data, method="POST", path="/api/assistant/context")
    request.group = group
    return request


@th.tier("bug")
@th.django_unit_test("#1553: a row only its owner may read opens for the owner and for nobody else")
def test_context_owner_only_row(opts):
    payload = {"model": "account.Notification", "pk": opts.notification.pk}
    resp = _post(opts, TEST_EMAIL_LIMITED, payload)
    assert_eq(resp.status_code, 200,
              f"the owner of an owner-only row must be able to open it, got {resp.status_code}: {resp.json}")
    conversation = _conversation(resp)
    assert_eq(conversation.user_id, opts.limited.pk, "the conversation belongs to the caller")
    assert_true("Owner-only row" in _content(conversation),
                f"the context should carry the row, got: {_content(conversation)[:200]}")

    for email in (TEST_EMAIL_ADMIN, TEST_EMAIL_MEMBER):
        resp = _post(opts, email, payload)
        assert_true(_is_not_found(resp),
                    f"SECURITY: {email} opened a row only its owner may read, or was told why not: "
                    f"{resp.status_code}: {resp.json}")


@th.tier("bug")
@th.django_unit_test("#1553: a missing row and a row the caller may not read answer alike")
def test_context_missing_and_unauthorized_answer_alike(opts):
    refused = _post(opts, TEST_EMAIL_LIMITED, {"model": "incident.Ticket", "pk": opts.groupless_ticket.pk})
    missing = _post(opts, TEST_EMAIL_LIMITED, {"model": "incident.Ticket", "pk": 999999999})
    for what, resp in (("a row the caller may not read", refused), ("a missing row", missing)):
        assert_true(_is_not_found(resp),
                    f"{what} must answer 404 with the one plain body, naming no model and no id, "
                    f"got {resp.status_code}: {resp.json}")
    assert_eq(dict(refused.json), dict(missing.json),
              "the two answers must be the same, key for key")
    # the same for someone who may read tickets, so the body is not a permission tell
    permitted = _post(opts, TEST_EMAIL_ADMIN, {"model": "incident.Ticket", "pk": 999999999})
    assert_eq(dict(permitted.json), dict(missing.json),
              f"a missing row answers the same for a permitted caller, got: {permitted.json}")


@th.tier("bug")
@th.django_unit_test("#1553: a grant held in one tenant opens that tenant's rows and no other's")
def test_context_member_grant_follows_the_source_tenant(opts):
    resp = _post(opts, TEST_EMAIL_TENANT, {"model": "incident.Ticket", "pk": opts.source_ticket.pk})
    assert_eq(resp.status_code, 200,
              f"view_security held in the ticket's own tenant must open it, got {resp.status_code}: {resp.json}")
    assert_eq(_conversation(resp).group_id, opts.source_group.pk,
              "the conversation is bound to the ticket's tenant")

    refused = (
        ("another tenant's ticket", {"model": "incident.Ticket", "pk": opts.other_ticket.pk}),
        ("another tenant's ticket, asked for under the tenant the grant is in",
         {"model": "incident.Ticket", "pk": opts.other_ticket.pk, "group": opts.source_group.pk}),
        ("a ticket that belongs to no tenant",
         {"model": "incident.Ticket", "pk": opts.groupless_ticket.pk, "group": opts.source_group.pk}),
    )
    for what, payload in refused:
        resp = _post(opts, TEST_EMAIL_TENANT, payload)
        assert_true(_is_not_found(resp),
                    f"SECURITY: a grant held in one tenant opened {what}, or was told why not: "
                    f"{resp.status_code}: {resp.json}")


@th.tier("bug")
@th.django_unit_test("#1553: the conversation takes the source row's tenant, not the group the caller sent")
def test_context_conversation_takes_the_source_tenant(opts):
    sent = opts.context_group.pk
    resp = _post(opts, TEST_EMAIL_ADMIN,
                 {"model": "incident.Ticket", "pk": opts.source_ticket.pk, "group": sent})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(_conversation(resp).group_id, opts.source_group.pk,
              "the conversation must be bound to the ticket's tenant, not the group in the request")

    resp = _post(opts, TEST_EMAIL_ADMIN,
                 {"model": "incident.Ticket", "pk": opts.groupless_ticket.pk, "group": sent})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.json}")
    assert_eq(_conversation(resp).group_id, None,
              "a ticket that belongs to no tenant gives a conversation with no group")


@th.tier("bug")
@th.django_unit_test("#1553: a narrower shape chosen by the row's own permission check is the shape used")
def test_context_hook_selected_graph_is_used(opts):
    payload = {"model": "account.Group", "pk": opts.source_group.pk}
    wide = _post(opts, TEST_EMAIL_ADMIN, payload)
    assert_eq(wide.status_code, 200, f"a holder of manage_groups opens the group, got: {wide.json}")
    assert_true(GROUP_MARKER in _content(_conversation(wide)),
                "precondition: the shape a group manager gets carries the group's settings")

    narrow = _post(opts, TEST_EMAIL_MEMBER, payload)
    assert_eq(narrow.status_code, 200,
              f"a plain member may open their own group, got {narrow.status_code}: {narrow.json}")
    content = _content(_conversation(narrow))
    assert_true("Source Tenant" in content, f"the basic shape carries the name, got: {content[:300]}")
    assert_true(GROUP_MARKER not in content,
                "SECURITY: a plain member's context carried the group's settings, which the "
                "basic shape leaves out")
    assert_eq(_conversation(narrow).group_id, None,
              "a group row has no owning group of its own, so its conversation has none")
    assert_true("**metadata**" not in content, "the basic shape has no settings key at all")

    outsider = _post(opts, TEST_EMAIL_LIMITED, payload)
    assert_true(_is_not_found(outsider),
                f"someone outside the group gets the plain not-found answer, got "
                f"{outsider.status_code}: {outsider.json}")


@th.tier("bug")
@th.django_unit_test("#1553: a caller cannot name the shape")
def test_context_refuses_a_caller_graph(opts):
    from mojo.apps.assistant.models import Conversation
    before = Conversation.objects.filter(user=opts.admin).count()
    for graph in ("detail", "default", "basic", "", None):
        resp = _post(opts, TEST_EMAIL_ADMIN,
                     {"model": "incident.Ticket", "pk": opts.source_ticket.pk, "graph": graph})
        assert_eq(resp.status_code, 400,
                  f"a request that names a graph ({graph!r}) must be refused, got {resp.status_code}: {resp.json}")
    assert_eq(Conversation.objects.filter(user=opts.admin).count(), before,
              "a refused request must not create a conversation")


@th.tier("bug")
@th.django_unit_test("#1553: a malformed model or id is a short 400, never a 500")
def test_context_malformed_input_is_a_bounded_400(opts):
    ticket = opts.source_ticket.pk
    bad = (
        ("a model that is a number", {"model": 123, "pk": ticket}),
        ("a model that is a list", {"model": ["incident", "Ticket"], "pk": ticket}),
        ("a model with three parts", {"model": "incident.Ticket.extra", "pk": ticket}),
        ("an empty model", {"model": "", "pk": ticket}),
        ("a model with an empty part", {"model": "incident.", "pk": ticket}),
        ("a very long model", {"model": "x" * 5000 + ".Ticket", "pk": ticket}),
        ("an unknown model", {"model": "incident.NoSuchThing", "pk": ticket}),
        ("a model with no REST interface open to it", {"model": "assistant.PendingAction", "pk": 1}),
        ("an id that is a word", {"model": "incident.Ticket", "pk": "abc"}),
        ("an empty id", {"model": "incident.Ticket", "pk": ""}),
        ("an id with a fraction", {"model": "incident.Ticket", "pk": 1.5}),
        ("an id that is true", {"model": "incident.Ticket", "pk": True}),
        ("an id that is a list", {"model": "incident.Ticket", "pk": [ticket]}),
        ("an id that is an object", {"model": "incident.Ticket", "pk": {"id": ticket}}),
        ("an id that is null", {"model": "incident.Ticket", "pk": None}),
    )
    for what, payload in bad:
        resp = _post(opts, TEST_EMAIL_ADMIN, payload)
        assert_eq(resp.status_code, 400, f"{what} must answer 400, got {resp.status_code}: {resp.json}")
        error = str(resp.json.get("error"))
        assert_true(len(error) <= 200, f"{what}: the error must be short, got {len(error)} characters")
        assert_true("xxxx" not in error and "NoSuchThing" not in error and "abc" not in error,
                    f"{what}: the error must not repeat what was sent, got: {error}")

    # A whole number no row can have is a missing row, not an error.
    resp = _post(opts, TEST_EMAIL_ADMIN, {"model": "incident.Ticket", "pk": 10 ** 40})
    assert_true(_is_not_found(resp),
                f"an id too large for the column is a missing row, got {resp.status_code}: {resp.json}")

    # A model the assistant is closed to is refused before any row is looked up.
    resp = _post(opts, TEST_EMAIL_ADMIN, {"model": "account.ApiKey", "pk": 999999999})
    assert_eq(resp.status_code, 403,
              f"a model closed to the assistant answers 403 whether or not the row exists, got: {resp.json}")


@th.tier("bug")
@th.django_unit_test("#1553: retries find the one conversation, and every retry is checked again")
def test_context_duplicates_converge_and_reauthorize(opts):
    from mojo.apps.account.models import GroupMember
    from mojo.apps.incident.models import Ticket
    ticket = Ticket.objects.create(
        title="[CTX-TEST] Retry ticket", description="retry", status="open", priority=5,
        category="security", user=opts.admin, group=opts.source_group)

    first = _post(opts, TEST_EMAIL_ADMIN, {"model": "incident.Ticket", "pk": ticket.pk})
    assert_eq(first.status_code, 200, f"Expected 200, got {first.status_code}: {first.json}")
    conversation = _conversation(first)
    assert_eq(conversation.metadata, {"source_model": "incident.ticket", "source_pk": ticket.pk},
              "the conversation records the row by its own model name and id")
    for what, payload in (
            ("the id as text", {"model": "incident.Ticket", "pk": str(ticket.pk)}),
            ("the model in another case", {"model": "incident.TICKET", "pk": ticket.pk})):
        again = _post(opts, TEST_EMAIL_ADMIN, payload)
        assert_eq(again.status_code, 200, f"Expected 200, got {again.status_code}: {again.json}")
        assert_eq((again.json.data.conversation_id, again.json.data.get("existing")),
                  (conversation.pk, True),
                  f"a retry with {what} must return the first conversation, got: {again.json}")

    # A retry is checked again: someone who no longer may read the row does
    # not get their old conversation back.
    payload = {"model": "account.Group", "pk": opts.source_group.pk}
    opened = _post(opts, TEST_EMAIL_MEMBER, payload)
    assert_eq(opened.status_code, 200, f"a member opens their group, got: {opened.json}")
    GroupMember.objects.filter(group=opts.source_group, user=opts.member).delete()
    try:
        after = _post(opts, TEST_EMAIL_MEMBER, payload)
        assert_true(_is_not_found(after),
                    f"SECURITY: a former member got their old conversation back, got "
                    f"{after.status_code}: {after.json}")
    finally:
        opts.source_group.add_member(opts.member)


@th.tier("bug")
@th.django_unit_test("#1553: the permission check leaves the request as it found it")
def test_context_request_state_is_restored(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.assistant.services import context
    from mojo.apps.incident.models import Ticket

    cases = (
        ("a member's own group", opts.member, Group, opts.source_group, (True, "basic")),
        ("a group the caller is outside", opts.limited, Group, opts.source_group, (False, None)),
        ("a ticket in the caller's tenant", opts.tenant_viewer, Ticket, opts.source_ticket, (True, None)),
        ("a ticket in another tenant", opts.tenant_viewer, Ticket, opts.other_ticket, (False, None)),
        ("a ticket in no tenant", opts.tenant_viewer, Ticket, opts.groupless_ticket, (False, None)),
    )
    for what, user, model, instance, expected in cases:
        request = _source_request(user, group=opts.context_group)
        assert_eq(context.authorize_source(request, model, instance), expected,
                  f"{what}: unexpected decision")
        assert_true(request.group is opts.context_group,
                    f"{what}: the request's group was left as {request.group}")
        assert_true("graph" not in request.DATA,
                    f"{what}: a graph was left on the request: {request.DATA.get('graph')}")

    # the whole endpoint, on each kind of answer
    answers = (
        ("a created conversation", opts.member, {"model": "account.Group", "pk": opts.source_group.pk}, 200),
        ("a row the caller may not read", opts.tenant_viewer,
         {"model": "incident.Ticket", "pk": opts.other_ticket.pk}, 404),
        ("a missing row", opts.admin, {"model": "incident.Ticket", "pk": 999999999}, 404),
        ("a malformed id", opts.admin, {"model": "incident.Ticket", "pk": "abc"}, 400),
        ("a closed model", opts.admin, {"model": "account.ApiKey", "pk": 1}, 403),
    )
    for what, user, payload, expected in answers:
        request = _source_request(user, group=opts.context_group, **payload)
        data, error, status = context.open_context(request)
        assert_eq(status, expected, f"{what}: expected {expected}, got {status} ({error})")
        assert_true(request.group is opts.context_group,
                    f"{what}: the request's group was left as {request.group}")
        assert_true("graph" not in request.DATA,
                    f"{what}: a graph was left on the request: {request.DATA.get('graph')}")


@th.tier("bug")
@th.django_unit_test("#1553: a refused read is reported, once an hour for one caller and model")
def test_context_refused_read_is_reported_once(opts):
    from mojo.apps.assistant.services import context
    from mojo.apps.incident import reporter
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    category = "assistant_context_denied"
    get_connection().delete(reporter.notice_key(category, f"{opts.tenant_viewer.pk}:incident.Ticket"))
    events = Event.objects.filter(category=category, uid=opts.tenant_viewer.pk)
    before = events.count()
    for ticket in (opts.other_ticket, opts.groupless_ticket, opts.other_ticket):
        request = _source_request(opts.tenant_viewer, model="incident.Ticket", pk=ticket.pk)
        assert_eq(context.open_context(request), (None, NOT_FOUND, 404),
                  "precondition: the read is refused")
    assert_eq(events.count(), before + 1,
              "three refused reads of one model by one caller are one event, not three")
    event = events.order_by("-id").first()
    assert_eq((event.level, event.model_name, event.scope), (4, "incident.Ticket", "assistant"),
              "the event is informational and names the model")
    assert_true(event.group_id is None, "the event carries no group")

    # a missing row is not a refusal and is not reported
    request = _source_request(opts.tenant_viewer, model="incident.Ticket", pk=999999999)
    context.open_context(request)
    assert_eq(events.count(), before + 1, "a missing row must not be reported as a refusal")


@th.tier("bug")
@th.django_unit_test("#1553: a shape the permission check names but the model does not have is refused")
def test_context_invalid_hook_selection_fails_closed(opts):
    from mojo.apps.assistant.services import context

    def model_selecting(value, view_perms=("view_things",)):
        class Stub:
            class RestMeta:
                GRAPHS = {"default": {"fields": ["id"]}, "narrow": {"fields": ["id"]}, "broken": None}

            @classmethod
            def get_rest_meta_prop(cls, name, default=None):
                return list(view_perms) if name == "VIEW_PERMS" else default

            @classmethod
            def rest_check_permission(cls, request, permission_keys, instance=None):
                request.DATA.set("graph", value)
                request.group = None
                return True
        return Stub

    for value in ("missing", "broken", "", None, 7, ["narrow"]):
        request = _source_request(opts.admin, group=opts.context_group)
        assert_eq(context.authorize_source(request, model_selecting(value), object()), (False, None),
                  f"SECURITY: a check that selected {value!r} must be refused, not served a wider shape")
        assert_true(request.group is opts.context_group and "graph" not in request.DATA,
                    f"selected {value!r}: the request was not put back")

    request = _source_request(opts.admin, group=opts.context_group)
    assert_eq(context.authorize_source(request, model_selecting("narrow"), object()), (True, "narrow"),
              "a shape the model does declare is passed on exactly")

    # A model that declares no view permissions is open to everyone over REST.
    # Here it stays closed, as it was before this change.
    request = _source_request(opts.admin, group=opts.context_group)
    assert_eq(context.authorize_source(request, model_selecting("narrow", view_perms=()), object()),
              (False, None),
              "SECURITY: a model that declares no view permissions was opened to the assistant")


@th.tier("bug")
@th.django_unit_test("#1553: the conversation and its first message are stored together or not at all")
def test_context_conversation_and_message_are_one_write(opts):
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant.services import context

    source = {"source_model": "ctx.atomic", "source_pk": 1}
    Conversation.objects.filter(metadata__source_model="ctx.atomic").delete()
    failed = False
    try:
        # a message with no text cannot be stored
        context.create_conversation(opts.admin, None, "[CTX-TEST] atomic", None, source)
    except Exception:
        failed = True
    assert_true(failed, "precondition: a message with no text must fail to store")
    assert_true(not Conversation.objects.filter(metadata__source_model="ctx.atomic").exists(),
                "a conversation was left behind with no first message")

    conversation = context.create_conversation(
        opts.admin, opts.source_group, "[CTX-TEST] atomic", "the context", source)
    try:
        assert_eq(Message.objects.filter(conversation=conversation, role="user").count(), 1,
                  "the stored conversation has its one first message")
        assert_eq(conversation.group_id, opts.source_group.pk, "the group given is the group stored")
    finally:
        conversation.delete()


@th.tier("bug")
@th.django_unit_test("#1553: a custom builder that returns no title or no text does not break the endpoint")
def test_context_builder_without_a_title(opts):
    from mojo.apps.assistant.models import Conversation, Skill
    from mojo.apps.assistant.services import context

    Skill.objects.filter(name="[CTX-TEST] Builder Skill").delete()
    skill = Skill.objects.create(user=opts.admin, tier="user", name="[CTX-TEST] Builder Skill", steps=[])
    payload = {"model": "assistant.Skill", "pk": skill.pk}
    try:
        context.register_context_builder("assistant.Skill", lambda instance: (None, "the body", None))
        data, error, status = context.open_context(_source_request(opts.admin, **payload))
        assert_eq(status, 200, f"a builder with no title must still open a conversation, got {status}: {error}")
        conversation = Conversation.objects.get(pk=data["conversation_id"])
        assert_eq(conversation.title, f"Skill #{skill.pk}",
                  "with no title from the builder the title is the model and its id")
        conversation.delete()

        for result in ((None, None, None), ("a title", "", None), (None, None, "the builder failed"),
                       ("a title", ["not", "text"], None)):
            context.register_context_builder("assistant.Skill", lambda instance, result=result: result)
            data, error, status = context.open_context(_source_request(opts.admin, **payload))
            assert_eq((data, error, status), (None, NOT_FOUND, 404),
                      f"a builder returning {result!r} must give the plain not-found answer")
            assert_true(not Conversation.objects.filter(
                user=opts.admin, metadata__source_model="assistant.skill",
                metadata__source_pk=skill.pk).exists(),
                f"a builder returning {result!r} must not leave a conversation")
    finally:
        context._CONTEXT_BUILDERS.pop("assistant.skill", None)
        skill.delete()
