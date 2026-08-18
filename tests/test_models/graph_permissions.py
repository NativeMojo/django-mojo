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
    # delete-before-create — tests run against a long-lived DB
    PlatformDeployment.objects.filter(actor="gp_test").delete()


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
    for model in apps.get_models():
        rest_meta = getattr(model, "RestMeta", None)
        graphs = getattr(rest_meta, "GRAPHS", None) if rest_meta else None
        if graphs and "default" not in graphs:
            offenders.append(f"{model.__module__}.{model.__name__}")
    th.assert_eq(
        offenders, [],
        f"these models declare GRAPHS but no 'default' graph: {offenders}")
