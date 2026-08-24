"""Item 2102 — graph resolution refuses instead of dumping, and graphs can gate.

Part A (bug): a model that declares graphs but no ``default`` must NOT fall
through to a whole-model dump when an unmapped graph is requested — it raises.
A model with no ``GRAPHS`` at all keeps its deliberate whole-model API.

Part B (feature): ``RestMeta.GRAPH_PERMISSIONS`` gates the *served* graph at the
REST boundary and the assistant model tools, additive to ``VIEW_PERMS``,
denying with a 403 that names the graph and the permissions.

These run in-process: setattr on a RestMeta does not cross into the testit
server process, so the model-layer behaviour is exercised by calling
``to_dict`` / the ``on_rest_*`` handlers directly (same pattern as
batch_row_permissions.py). Request fakes are objict; the identities on them are
real model instances — the ``is_request_user`` marker lives on the identity.
"""
import uuid as _uuid

import objict
from testit import helpers as th

TESTIT_TIER = "core"  # #2792 tier curation


SHA = "a" * 40


def _platform_row():
    """One saved PlatformDeployment carrying a privileged stderr tail."""
    from mojo.apps.edge.models import PlatformDeployment
    row = PlatformDeployment.objects.create(
        sha=SHA, actor="gp_test", source="test", request_key=str(_uuid.uuid4()),
        status="failed", frozen_roster=["edge-a-engine"], transitions=[],
        node_evidence=[{
            "runner": "edge-a-engine", "state": "failed",
            "detail": {"phase": "update_script", "exit": 23, "stderr_tail": [
                "psql: postgres://deploy:hunter2@db.internal/app",
                "ERROR: relation already exists"]}}])
    return row


@th.django_unit_setup()
def setup_graph_permissions(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.account.models import User, Group
    from mojo.apps.chat.models import ChatRoom
    # delete-before-create — tests run against a long-lived DB
    PlatformDeployment.objects.filter(actor="gp_test").delete()
    ChatRoom.objects.filter(name__startswith="graphperm_").delete()
    User.objects.filter(email__startswith="graphperm_").delete()
    Group.objects.filter(name__startswith="graphperm_grp_").delete()


def _req(user, graph=None, group=None, method="GET", data=None):
    """A request fake in the shape _evaluate_permission expects. objict is fine
    for the request; the identity on it must be a real User (the
    ``is_request_user`` marker lives there)."""
    payload = dict(data or {})
    if graph is not None:
        payload["graph"] = graph
    req = objict.objict()
    req.user = user
    req.DATA = objict.objict(payload)
    req.QUERY_PARAMS = objict.objict()
    req.method = method
    req.group = group
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/graph_permissions_test"
    req.META = {}
    req.api_key = None
    return req


def _global_user(tag, perms):
    from mojo.apps.account.models import User
    email = f"graphperm_{tag}_{_uuid.uuid4().hex[:8]}@test.com"
    user = User.objects.create_user(username=email, email=email, password="testit##mojo")
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    if perms:
        user.add_permission(list(perms))
        user.save()
    return user


@th.django_unit_test(
    "a model declaring graphs but no default raises, never dumps every field")
def test_missing_default_graph_raises(opts):
    """Part A regression. With the requested graph AND `default` both undefined
    on a model that DOES declare graphs, the serializer must fail loud. On the
    pre-fix code this returned a whole-model dump (every field, including
    request_key) — so the raise assertion fails while the bug is present."""
    from mojo.apps.edge.models import PlatformDeployment
    from mojo import errors as me

    row = _platform_row()
    original = PlatformDeployment.RestMeta.GRAPHS
    # A partial graph set with no `default` — the exact misconfiguration the
    # guard protects against: a model wired, or its `default` removed in a
    # cleanup, with no diff at the graph definition to notice.
    PlatformDeployment.RestMeta.GRAPHS = {"basic": original["basic"]}
    try:
        basic = row.to_dict(graph="basic")
        th.assert_true("id" in basic, "a defined graph must still serialize")
        assert "request_key" not in basic, \
            "the basic graph must not serve fields outside its own list"

        for graph in ("default", "list", "unmapped-name"):
            raised = False
            try:
                row.to_dict(graph=graph)
            except me.RestErrorException:
                raised = True
            th.assert_true(
                raised,
                f"graph={graph!r} with no default must raise, not dump every "
                f"field on the model")
    finally:
        PlatformDeployment.RestMeta.GRAPHS = original


@th.django_unit_test(
    "a model with no GRAPHS keeps its deliberate whole-model serialization")
def test_no_graphs_model_still_serializes_whole(opts):
    """The no-`GRAPHS` case is a different, deliberate semantic (a VIEW_PERMS
    -gated whole-model API) and must stay forgiving — the part-A raise only
    applies to models that DO declare graphs."""
    from mojo.apps.edge.models import PlatformDeployment

    row = _platform_row()
    original = PlatformDeployment.RestMeta.GRAPHS
    PlatformDeployment.RestMeta.GRAPHS = {}
    try:
        data = row.to_dict(graph="default")
        th.assert_true(isinstance(data, dict) and "id" in data,
                       "an empty GRAPHS map must fall back to whole-object "
                       "serialization, not raise")
    finally:
        PlatformDeployment.RestMeta.GRAPHS = original


@th.django_unit_test(
    "every REST model that declares graphs also defines a default graph")
def test_all_graph_models_define_default(opts):
    """The cheap guard-rail: this can never silently regress to non-zero. A
    model that ships graphs without `default` would be unserializable on its
    detail path the moment it is wired."""
    from django.apps import apps

    offenders = []
    bad_graph_perms = []
    for model in apps.get_models():
        rest_meta = getattr(model, "RestMeta", None)
        graphs = getattr(rest_meta, "GRAPHS", None) if rest_meta else None
        if graphs and "default" not in graphs:
            offenders.append(f"{model.__module__}.{model.__name__}")
        graph_perms = getattr(rest_meta, "GRAPH_PERMISSIONS", None) if rest_meta else None
        if graph_perms:
            for name in graph_perms:
                if not graphs or name not in graphs:
                    bad_graph_perms.append(f"{model.__name__}.{name}")
    th.assert_eq(
        offenders, [],
        f"these models declare GRAPHS but no 'default' graph: {offenders}")
    th.assert_eq(
        bad_graph_perms, [],
        f"these GRAPH_PERMISSIONS entries name a graph the model does not "
        f"define: {bad_graph_perms}")


# ---------------------------------------------------------------------------
# Part B — GRAPH_PERMISSIONS and boundary resolution
# ---------------------------------------------------------------------------


@th.django_unit_test(
    "an undefined special graph is refused 400; an undefined common name falls back")
def test_graph_name_resolution(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo import errors as me

    user = _global_user("resolver", ["manage_platform"])

    th.assert_eq(
        PlatformDeployment.rest_resolve_graph_or_raise(_req(user), "basic"),
        "basic", "a defined graph must resolve to itself")
    # An undefined COMMON name resolves without raising — the serializer falls
    # back to `default`. The returned name is the requested one (envelope
    # stays byte-identical).
    th.assert_eq(
        PlatformDeployment.rest_resolve_graph_or_raise(_req(user), "detail"),
        "detail", "an undefined common name must fall back, not refuse")
    # An undefined SPECIAL name is a deliberate request for a view the model
    # does not have — refuse it rather than mislead / give a prober a 200.
    raised = False
    try:
        PlatformDeployment.rest_resolve_graph_or_raise(_req(user), "no_such_view")
    except me.ValueException as e:
        raised = True
        th.assert_eq(e.status, 400, "an unknown special graph is a client 400")
    th.assert_true(raised, "an undefined special graph name must be refused")


@th.django_unit_test(
    "GRAPH_PERMISSIONS gates the served graph, additive to VIEW_PERMS")
def test_graph_permission_gate(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo import errors as me

    row = _platform_row()
    # view_platform satisfies VIEW_PERMS but NOT the admin-graph gate.
    viewer = _global_user("viewer", ["view_platform"])
    admin = _global_user("admin", ["manage_platform"])

    original = getattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS", None)
    PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = {
        "admin": ["manage_platform", "admin"]}
    try:
        denied = False
        try:
            PlatformDeployment.on_rest_handle_get(_req(viewer, graph="admin"), row)
        except me.PermissionDeniedException as e:
            denied = True
            th.assert_eq(e.event_type, "graph_permission_denied",
                         "denial must be categorized as a graph-permission denial")
            th.assert_true("admin" in e.reason,
                           "the 403 must name the graph the caller was refused")
        th.assert_true(denied,
                       "view_platform alone must not unlock the gated admin graph")

        resp = PlatformDeployment.on_rest_handle_get(_req(admin, graph="admin"), row)
        th.assert_eq(getattr(resp, "status_code", None), 200,
                     "a manage_platform holder must be served the admin graph")

        # An ungated graph stays readable to any VIEW_PERMS holder.
        resp2 = PlatformDeployment.on_rest_handle_get(_req(viewer, graph="default"), row)
        th.assert_eq(getattr(resp2, "status_code", None), 200,
                     "an ungated graph must remain readable to a VIEW_PERMS holder")
    finally:
        if original is None:
            if hasattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS"):
                delattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS")
        else:
            PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = original


@th.django_unit_test(
    "the gate checks the graph actually served, not the requested name")
def test_graph_permission_effective_graph(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo import errors as me

    row = _platform_row()
    viewer = _global_user("effviewer", ["view_platform"])

    original = getattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS", None)
    # Gate `default` itself. A request for an undefined common name falls back
    # to `default`, so it must be gated against default's perms — not waved
    # through because the requested name had no entry of its own.
    PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = {
        "default": ["manage_platform", "admin"]}
    try:
        denied = False
        try:
            PlatformDeployment.on_rest_handle_get(_req(viewer, graph="detail"), row)
        except me.PermissionDeniedException:
            denied = True
        th.assert_true(
            denied,
            "a common name falling back to a gated default must be gated too")
    finally:
        if original is None:
            if hasattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS"):
                delattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS")
        else:
            PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = original


@th.django_unit_test(
    "a denied graph on a write refuses BEFORE the row is mutated")
def test_write_preflight_no_mutation(opts):
    """The graph gate on save/create/batch runs before the mutation, so a
    caller can never have a write applied and then receive the graph 403."""
    from mojo.apps.account.models import User, Group, GroupMember
    from mojo.apps.chat.models import ChatRoom
    from mojo import errors as me

    tag = _uuid.uuid4().hex[:8]
    group = Group.objects.create(name=f"graphperm_grp_{tag}", is_active=True)
    user = User.objects.create_user(
        username=f"graphperm_saver_{tag}@test.com",
        email=f"graphperm_saver_{tag}@test.com", password="testit##mojo")
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    member, _ = GroupMember.objects.get_or_create(user=user, group=group)
    member.permissions = {"manage_chat": True}
    member.save()
    room = ChatRoom.objects.create(group=group, name="graphperm_room_orig")

    original = getattr(ChatRoom.RestMeta, "GRAPH_PERMISSIONS", None)
    # A perm nobody holds, gating the default response graph.
    ChatRoom.RestMeta.GRAPH_PERMISSIONS = {"default": ["graphperm_absent"]}
    try:
        req = _req(user, graph="default", group=group, method="POST",
                   data={"name": "graphperm_room_CHANGED"})
        denied = False
        try:
            ChatRoom.on_rest_handle_save(req, room)
        except me.PermissionDeniedException as e:
            denied = True
            th.assert_eq(e.event_type, "graph_permission_denied",
                         "the write must be refused by the graph gate")
        th.assert_true(denied, "a gated graph the saver lacks must refuse the write")

        fresh = ChatRoom.objects.get(pk=room.pk)
        th.assert_eq(fresh.name, "graphperm_room_orig",
                     "the row must be UNCHANGED — the refusal preceded the write")
    finally:
        if original is None:
            if hasattr(ChatRoom.RestMeta, "GRAPH_PERMISSIONS"):
                delattr(ChatRoom.RestMeta, "GRAPH_PERMISSIONS")
        else:
            ChatRoom.RestMeta.GRAPH_PERMISSIONS = original


@th.django_unit_test(
    "the assistant query_model tool gates the caller-supplied graph too")
def test_assistant_query_model_graph_gate(opts):
    """query_model serializes outside the REST read sites, so it must gate the
    graph itself or become the bypass for GRAPH_PERMISSIONS."""
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.assistant.services.tools.models import _tool_query_model

    _platform_row()
    viewer = _global_user("aiviewer", ["view_admin", "view_platform"])
    admin = _global_user("aiadmin", ["view_admin", "manage_platform"])

    original = getattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS", None)
    PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = {
        "admin": ["manage_platform", "admin"]}
    try:
        params = {"app_name": "edge", "model_name": "PlatformDeployment",
                  "graph": "admin"}
        denied = _tool_query_model(dict(params), viewer)
        th.assert_true("error" in denied,
                       f"view_platform user must be refused the admin graph: {denied}")

        served = _tool_query_model(dict(params), admin)
        th.assert_true("error" not in served,
                       f"manage_platform user must be served: {served}")

        # An unknown special graph is a 400-shaped error, not a silent 200.
        bad = _tool_query_model(
            {"app_name": "edge", "model_name": "PlatformDeployment",
             "graph": "no_such_view"}, admin)
        th.assert_true("error" in bad,
                       "an unknown special graph must be an error, not served")
    finally:
        if original is None:
            if hasattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS"):
                delattr(PlatformDeployment.RestMeta, "GRAPH_PERMISSIONS")
        else:
            PlatformDeployment.RestMeta.GRAPH_PERMISSIONS = original
